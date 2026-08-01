from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import atlas_host_operations.controller as controller_module
import pytest
from atlas_host_operations.artifacts import set_fingerprint, validate_source_bindings
from atlas_host_operations.configurators import (
    AnsibleHostConfigurator,
    FakeHostConfigurator,
)
from atlas_host_operations.controller import (
    HostController,
    InlinePhaseExecutor,
    PhaseExecution,
    _operation_host_plan,
    _provider_evidence,
    _run_phase,
    _terminal_evidence,
    build_host_plan,
    configurator_for_plan,
    execute_phase,
    main,
    provider_for_plan,
    reconcile_job_main,
)
from atlas_host_operations.errors import (
    AdapterError,
    HostOperationError,
    PlanError,
    RegistryAuthenticationError,
    RegistryConflictError,
    RegistryError,
)
from atlas_host_operations.lifecycle import (
    OperationStatus,
    ProvisioningPhase,
    StepStatus,
)
from atlas_host_operations.models import (
    CheckResult,
    GitSourceReference,
    HostOperationEvidence,
    HostOperationPlan,
    HostStatus,
    ProviderEvidence,
    ProviderObservation,
    RegistryAuthority,
    StepResult,
    VerificationResult,
)
from atlas_host_operations.providers import (
    FakeCloudProvider,
    HostContext,
    ProxmoxHostProvider,
)
from atlas_host_operations.readiness import HostReadinessChecker
from atlas_host_operations.registry import (
    HTTPRegistryClient,
    HTTPResponse,
    InMemoryRegistryClient,
    UrllibTransport,
    _operation_model,
    _registry_step_status,
)
from atlas_host_operations.subprocesses import SubprocessRunner, _timeout_text
from pydantic import ValidationError

from .test_host_operations_support import (
    ScriptedTransport,
    make_host_fixture,
    operation_payload,
    resource_payload,
    response,
)


def _inline_controller(fixture) -> HostController:
    return HostController(
        fixture.registry,
        InlinePhaseExecutor(
            fixture.registry,
            fixture.provider,
            fixture.configurator,
            fixture.readiness,
        ),
        provider=fixture.provider,
        configurator=fixture.configurator,
        readiness=fixture.readiness,
    )


def _client(transport: ScriptedTransport) -> HTTPRegistryClient:
    return HTTPRegistryClient(
        "https://registry.example.test",
        access_jwt="jwt",
        transport=transport,
    )


def _write_plan(path: Path, plan: HostOperationPlan) -> Path:
    path.write_text(json.dumps(plan.as_artifact()), encoding="utf-8")
    return path


def test_remaining_model_validators_and_artifact_methods(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    with pytest.raises(ValidationError, match="project path"):
        GitSourceReference(path="relative", gitCommit="0" * 40, gitDirty=False)
    with pytest.raises(ValidationError, match="object ID"):
        GitSourceReference(path="/tmp/project", gitCommit="bad", gitDirty=False)
    data = plan.as_artifact()
    data["metadata"]["target"] = "other"
    data["metadata"].pop("fingerprint")
    with pytest.raises(ValidationError, match="plan target"):
        HostOperationPlan.model_validate(data)
    data = plan.as_artifact()
    data["configuration"]["target"] = "other"
    data["metadata"].pop("fingerprint")
    with pytest.raises(ValidationError, match="configuration target"):
        HostOperationPlan.model_validate(data)
    status = HostStatus(
        operationId=None,
        planId=plan.metadata.plan_id,
        resourceId=plan.resource.id,
        operationStatus="planned",
        resourceLifecycle="absent",
        currentPhase="validate",
    )
    assert status.as_artifact()["currentPhase"] == "validate"


def test_source_binding_detects_new_commit_without_dirty_state(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    marker = fixture.project / "marker"
    marker.write_text("new", encoding="utf-8")
    subprocess.run(["git", "-C", str(fixture.project), "add", "marker"], check=True)
    subprocess.run(
        ["git", "-C", str(fixture.project), "commit", "-qm", "second"],
        check=True,
    )
    with pytest.raises(PlanError, match="Git commit changed"):
        validate_source_bindings(plan)


def test_build_plan_reports_failed_checks_and_inventory_list_shape(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    inventory = fixture.project / "inventories" / "site01" / "hosts.yml"
    inventory.write_text("all:\n  hosts:\n    - web01\n", encoding="utf-8")

    class FailedChecks(FakeCloudProvider):
        def validate(self, context):
            return [
                CheckResult(
                    name="provider.message", status="failed", message="explicit"
                ),
                CheckResult(name="provider.name", status="failed"),
            ]

    with pytest.raises(AdapterError, match=r"explicit; provider.name"):
        build_host_plan(
            fixture.host_spec,
            registry=fixture.registry,
            provider=FailedChecks(name="fake-cloud"),
            configurator=fixture.configurator,
        )


def test_resume_completed_status_without_resource_and_failed_verify(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    controller = _inline_controller(fixture)
    controller.apply(plan, confirm=plan.metadata.plan_id)
    assert controller.resume(plan, confirm=plan.metadata.plan_id).exit_code == 0
    operation_id = next(iter(fixture.registry.operations))
    fixture.readiness.status = "failed"
    assert controller.verify(operation_id).status == "failed"
    fixture.registry.resources.pop(plan.resource.id)
    assert controller.status(operation_id).resource_lifecycle == "absent"


def test_run_with_no_execution_phases_and_rollback_without_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    operation = fixture.registry.create_operation(plan)
    controller = _inline_controller(fixture)
    monkeypatch.setattr(controller_module, "EXECUTION_PHASES", ())
    with pytest.raises(PlanError, match="no executable phases"):
        controller._run(plan, operation, resume=False)
    monkeypatch.setattr(
        controller_module,
        "EXECUTION_PHASES",
        tuple(ProvisioningPhase)[1:],
    )

    second = make_host_fixture(tmp_path / "second")
    second_plan = second.plan()
    second_operation = second.registry.create_operation(second_plan)
    second.registry.acquire_locks(
        second_operation.id,
        ["resource/host-web01"],
    )
    execute_phase(
        ProvisioningPhase.RESERVE,
        second_plan,
        second_operation.id,
        registry=second.registry,
        provider=second.provider,
        configurator=second.configurator,
        readiness=second.readiness,
        resume=False,
    )
    execute_phase(
        ProvisioningPhase.ALLOCATE,
        second_plan,
        second_operation.id,
        registry=second.registry,
        provider=second.provider,
        configurator=second.configurator,
        readiness=second.readiness,
        resume=False,
    )
    plan_path = _write_plan(second.root / "plan.json", second_plan)
    result = _inline_controller(second).rollback(
        str(plan_path),
        confirm=second_plan.metadata.plan_id,
    )
    assert result.exit_code == 0
    assert "remove-binding" not in second.registry.call_log


class _ConflictOnReserve(InMemoryRegistryClient):
    def reserve_resource(self, operation_id, resource, authority):
        raise RegistryConflictError("conflict")


class _RegistryFailureOnReserve(InMemoryRegistryClient):
    def reserve_resource(self, operation_id, resource, authority):
        raise RegistryError("unavailable")


class _RegistryAuthenticationFailureOnReserve(InMemoryRegistryClient):
    def reserve_resource(self, operation_id, resource, authority):
        raise RegistryAuthenticationError("forbidden")


def test_phase_failure_mapping_for_registry_errors(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    for registry, expected in (
        (
            _ConflictOnReserve(list(fixture.registry.resources.values())),
            5,
        ),
        (
            _RegistryFailureOnReserve(list(fixture.registry.resources.values())),
            5,
        ),
        (
            _RegistryAuthenticationFailureOnReserve(
                list(fixture.registry.resources.values())
            ),
            4,
        ),
    ):
        operation = registry.create_operation(plan)
        registry.acquire_locks(operation.id, ["resource/host-web01"])
        result = execute_phase(
            ProvisioningPhase.RESERVE,
            plan,
            operation.id,
            registry=registry,
            provider=fixture.provider,
            configurator=fixture.configurator,
            readiness=fixture.readiness,
            resume=False,
        )
        assert result.exit_code == expected
        assert result.evidence.status is StepStatus.FAILED


def test_revalidation_detects_missing_provider_binding_and_activation(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    controller = _inline_controller(fixture)
    controller.apply(plan, confirm=plan.metadata.plan_id)
    operation_id = next(iter(fixture.registry.operations))
    operation = fixture.registry.operations[operation_id]
    fixture.registry.operations[operation_id] = operation.model_copy(
        update={"status": OperationStatus.NEEDS_RECONCILE.value}
    )
    fixture.registry.acquire_locks(operation_id, ["resource/host-web01"])
    fixture.provider.exists = False
    allocate = execute_phase(
        ProvisioningPhase.ALLOCATE,
        plan,
        operation_id,
        registry=fixture.registry,
        provider=fixture.provider,
        configurator=fixture.configurator,
        readiness=fixture.readiness,
        resume=True,
    )
    assert allocate.exit_code == 6

    fixture.provider.exists = True
    fixture.provider.running = True
    resource = fixture.registry.resources[plan.resource.id]
    fixture.registry.resources[plan.resource.id] = resource.model_copy(
        update={"binding": None}
    )
    bind = execute_phase(
        ProvisioningPhase.BIND,
        plan,
        operation_id,
        registry=fixture.registry,
        provider=fixture.provider,
        configurator=fixture.configurator,
        readiness=fixture.readiness,
        resume=True,
    )
    assert bind.exit_code == 5

    resource = fixture.registry.resources[plan.resource.id]
    fixture.registry.resources[plan.resource.id] = resource.model_copy(
        update={"lifecycle_state": "provisioning"}
    )
    activate = execute_phase(
        ProvisioningPhase.ACTIVATE,
        plan,
        operation_id,
        registry=fixture.registry,
        provider=fixture.provider,
        configurator=fixture.configurator,
        readiness=fixture.readiness,
        resume=True,
    )
    assert activate.exit_code == 5


def test_internal_operation_step_guard_edges(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    controller = _inline_controller(fixture)
    operation = fixture.registry.create_operation(plan)
    fixture.registry.acquire_locks(
        operation.id,
        ["resource/host-web01"],
    )
    steps = list(operation.steps)
    validate_index = next(
        index
        for index, step in enumerate(steps)
        if step.name == ProvisioningPhase.VALIDATE.value
    )
    steps[validate_index] = steps[validate_index].model_copy(update={"status": "running"})
    running_validation = operation.model_copy(update={"steps": steps})
    fixture.registry.operations[operation.id] = running_validation
    controller._record_plan_validation(plan, running_validation)

    updated = fixture.registry.operations[operation.id]
    validate_step = updated.steps[validate_index]
    assert validate_step.status == "succeeded"
    evidence = HostOperationEvidence.model_validate(validate_step.evidence)
    revision = validate_step.revision
    idempotent_authority = fixture.registry.renew_locks(operation.id)
    fixture.registry.record_step(operation.id, evidence, idempotent_authority)
    assert fixture.registry.operations[operation.id].steps[validate_index].revision == revision

    without_allocate = updated.model_copy(
        update={
            "steps": [
                step
                for step in updated.steps
                if step.name != ProvisioningPhase.ALLOCATE.value
            ]
        }
    )
    fixture.registry.operations[operation.id] = without_allocate
    with pytest.raises(RegistryConflictError, match="step is missing"):
        execute_phase(
            ProvisioningPhase.ALLOCATE,
            plan,
            operation.id,
            registry=fixture.registry,
            provider=fixture.provider,
            configurator=fixture.configurator,
            readiness=fixture.readiness,
            resume=True,
        )

    current_authority = fixture.registry.renew_locks(operation.id)
    with pytest.raises(RegistryConflictError, match="cannot be resumed"):
        controller_module._ensure_step_running(
            plan,
            operation.id,
            validate_step,
            fixture.registry,
            current_authority,
            datetime.now(UTC),
        )
    with pytest.raises(RegistryConflictError, match="operation not found"):
        controller_module._mark_needs_reconcile(
            plan,
            "missing",
            evidence,
            fixture.registry,
            current_authority,
        )

    terminal_steps = [step.model_copy(update={"status": "succeeded"}) for step in steps]
    fixture.registry.operations[operation.id] = updated.model_copy(
        update={"status": "running", "steps": terminal_steps}
    )
    with pytest.raises(RegistryConflictError, match="no writable step"):
        controller_module._mark_needs_reconcile(
            plan,
            operation.id,
            evidence,
            fixture.registry,
            current_authority,
        )


def test_private_phase_and_artifact_validation_failures(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    authority = RegistryAuthority(
        operationId="op-1",
        lockScope="resource/host-web01",
        fencingToken=1,
        operationRevision=1,
        resourceRevision=1,
    )
    with pytest.raises(PlanError, match="not executable"):
        _run_phase(
            ProvisioningPhase.VALIDATE,
            HostContext(plan),
            "op-1",
            fixture.registry,
            fixture.provider,
            fixture.configurator,
            fixture.readiness,
            authority,
        )
    with pytest.raises(RegistryConflictError, match="not found"):
        _run_phase(
            ProvisioningPhase.PROVIDER_VERIFY,
            HostContext(plan),
            "missing",
            fixture.registry,
            fixture.provider,
            fixture.configurator,
            fixture.readiness,
            authority,
        )

    operation = fixture.registry.create_operation(plan)
    with pytest.raises(PlanError, match="allocation evidence"):
        _provider_evidence(operation)
    bad_step = operation.steps[2].model_copy(
        update={
            "status": "succeeded",
            "evidence": {"details": {"providerEvidence": {"bad": True}}},
        }
    )
    invalid_operation = operation.model_copy(
        update={"steps": [*operation.steps[:2], bad_step, *operation.steps[3:]]}
    )
    with pytest.raises(PlanError, match="evidence is invalid"):
        _provider_evidence(invalid_operation)
    missing_previous = bad_step.model_copy(
        update={
            "status": "blocked",
            "evidence": {"details": {"previousEvidence": "invalid"}},
        }
    )
    with pytest.raises(PlanError, match="allocation evidence is missing"):
        _provider_evidence(
            operation.model_copy(
                update={
                    "steps": [
                        *operation.steps[:2],
                        missing_previous,
                        *operation.steps[3:],
                    ]
                }
            )
        )
    with pytest.raises(PlanError, match="does not contain"):
        _operation_host_plan(operation.model_copy(update={"plan": {}}))
    with pytest.raises(PlanError, match="invalid host plan"):
        _operation_host_plan(
            operation.model_copy(update={"plan": {"intent": {"hostPlan": {}}}})
        )


def test_factory_success_and_step_result_failure(tmp_path: Path) -> None:
    plan = make_host_fixture(tmp_path).plan()
    proxmox = plan.model_copy(
        update={
            "provider": plan.provider.model_copy(
                update={"adapter": "proxmox", "resource_type": "proxmox.qemu"}
            )
        }
    )
    ansible = plan.model_copy(
        update={
            "configuration": plan.configuration.model_copy(
                update={"adapter": "ansible"}
            )
        }
    )
    assert isinstance(provider_for_plan(proxmox), ProxmoxHostProvider)
    with pytest.raises(AdapterError, match="Resource type"):
        provider_for_plan(
            proxmox.model_copy(
                update={
                    "provider": proxmox.provider.model_copy(
                        update={"resource_type": "other"}
                    )
                }
            )
        )
    assert isinstance(configurator_for_plan(ansible), AnsibleHostConfigurator)

    class FailedResult(FakeHostConfigurator):
        def bootstrap(self, context):
            return StepResult(status="failed", message="returned failure")

    fixture = make_host_fixture(tmp_path / "failure")
    failure_plan = fixture.plan()
    operation = fixture.registry.create_operation(failure_plan)
    fixture.registry.acquire_locks(operation.id, ["resource/host-web01"])
    execute_phase(
        ProvisioningPhase.RESERVE,
        failure_plan,
        operation.id,
        registry=fixture.registry,
        provider=fixture.provider,
        configurator=fixture.configurator,
        readiness=fixture.readiness,
        resume=False,
    )
    execute_phase(
        ProvisioningPhase.ALLOCATE,
        failure_plan,
        operation.id,
        registry=fixture.registry,
        provider=fixture.provider,
        configurator=fixture.configurator,
        readiness=fixture.readiness,
        resume=False,
    )
    result = execute_phase(
        ProvisioningPhase.BOOTSTRAP,
        failure_plan,
        operation.id,
        registry=fixture.registry,
        provider=fixture.provider,
        configurator=FailedResult(),
        readiness=fixture.readiness,
        resume=False,
    )
    assert result.exit_code == 4


def test_reconcile_missing_and_failed_observation_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    monkeypatch.setattr(controller_module, "read_plan", lambda path: plan)
    monkeypatch.setattr(
        controller_module, "load_registry_client", lambda _plan: fixture.registry
    )
    monkeypatch.setattr(
        controller_module, "provider_for_plan", lambda _plan: fixture.provider
    )
    assert reconcile_job_main(["--plan", "-", "--operation", "missing"]) == 2
    assert "not found" in capsys.readouterr().err

    operation = fixture.registry.create_operation(plan)
    authority = fixture.registry.acquire_locks(
        operation.id,
        ["resource/host-web01"],
    )
    fixture.registry.start_operation(operation.id, authority)
    without_allocation = operation.model_copy(
        update={
            "steps": [
                step
                for step in operation.steps
                if step.name != ProvisioningPhase.ALLOCATE.value
            ]
        }
    )
    fixture.registry.operations[operation.id] = without_allocation.model_copy(
        update={"status": "running"}
    )
    assert reconcile_job_main(["--plan", "-", "--operation", operation.id]) == 5
    assert "allocation step is missing" in capsys.readouterr().err
    fixture.registry.operations[operation.id] = fixture.registry.operations[
        operation.id
    ].model_copy(update={"steps": operation.steps})
    fixture.provider.exists = False
    fixture.provider.running = False
    assert reconcile_job_main(["--plan", "-", "--operation", operation.id]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"

    uncertain = make_host_fixture(tmp_path / "uncertain")
    uncertain_plan = uncertain.plan()
    uncertain_operation = uncertain.registry.create_operation(uncertain_plan)
    uncertain_authority = uncertain.registry.acquire_locks(
        uncertain_operation.id,
        ["resource/host-web01"],
    )
    uncertain.registry.start_operation(
        uncertain_operation.id,
        uncertain_authority,
    )
    uncertain.provider.exists = True
    uncertain.provider.running = True
    uncertain.provider.ownership_marker = False
    monkeypatch.setattr(controller_module, "read_plan", lambda path: uncertain_plan)
    monkeypatch.setattr(
        controller_module,
        "load_registry_client",
        lambda _plan: uncertain.registry,
    )
    monkeypatch.setattr(
        controller_module,
        "provider_for_plan",
        lambda _plan: uncertain.provider,
    )
    assert (
        reconcile_job_main(["--plan", "-", "--operation", uncertain_operation.id]) == 6
    )
    assert json.loads(capsys.readouterr().out)["status"] == "needs-reconcile"


class _FakeMainController:
    def __init__(self, plan: HostOperationPlan) -> None:
        self.plan = plan
        now = datetime.now(UTC)
        self.evidence = HostOperationEvidence(
            apiVersion="atlas.host-operation/v1",
            kind="HostOperationEvidence",
            operationId="op-1",
            planId=plan.metadata.plan_id,
            resourceId=plan.resource.id,
            phase="activate",
            status="succeeded",
            startedAt=now,
            finishedAt=now,
            attempt=1,
        )

    def apply(self, plan, *, confirm):
        return PhaseExecution(self.evidence, 0)

    def status(self, target):
        return HostStatus(
            operationId="op-1",
            planId=self.plan.metadata.plan_id,
            resourceId=self.plan.resource.id,
            operationStatus="running",
            resourceLifecycle="provisioning",
            currentPhase="wait-ready",
        )

    def _resolve_plan_and_operation(self, target, *, require_operation):
        return self.plan, object()

    def resume(self, plan, *, confirm):
        return PhaseExecution(self.evidence, 0)

    def verify(self, target):
        return VerificationResult(status="passed")

    def rollback(self, target, *, confirm):
        return PhaseExecution(self.evidence, 0)


def test_hostctl_main_all_public_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    plan_path = _write_plan(tmp_path / "plan.json", plan)
    fake = _FakeMainController(plan)
    monkeypatch.setattr(controller_module, "build_host_plan", lambda path: plan)
    monkeypatch.setattr(controller_module, "read_plan", lambda path: plan)
    monkeypatch.setattr(
        controller_module, "load_registry_client", lambda _plan: object()
    )
    monkeypatch.setattr(
        controller_module, "HostController", lambda *args, **kwargs: fake
    )

    assert main(["plan", str(fixture.host_spec)]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "HostOperationPlan"
    assert main(["apply", str(plan_path), "--confirm", plan.metadata.plan_id]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "HostOperationEvidence"
    assert main(["status", str(plan_path)]) == 0
    assert json.loads(capsys.readouterr().out)["operationStatus"] == "running"
    assert main(["resume", str(plan_path), "--confirm", plan.metadata.plan_id]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "activate"
    assert main(["verify", str(plan_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"
    assert main(["rollback", str(plan_path), "--confirm", plan.metadata.plan_id]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "activate"

    fake.verify = lambda target: VerificationResult(status="failed")
    assert main(["verify", str(plan_path)]) == 1
    capsys.readouterr()

    monkeypatch.setenv("ATLAS_REGISTRY_PROFILE", str(fixture.registry_profile))
    monkeypatch.setattr(
        controller_module.HTTPRegistryClient,
        "from_profile",
        lambda profile: object(),
    )
    assert main(["status", "op-1"]) == 0
    capsys.readouterr()
    monkeypatch.delenv("ATLAS_REGISTRY_PROFILE")
    assert main(["status", "op-1"]) == 2
    assert "ATLAS_REGISTRY_PROFILE" in capsys.readouterr().err


def test_registry_http_and_in_memory_remaining_error_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()

    for find in (
        lambda client: client.find_operation_by_idempotency_key("none"),
        lambda client: client.find_operation_for_resource("other"),
    ):
        client = HTTPRegistryClient(
            "https://registry.example.test",
            access_jwt="jwt",
            transport=ScriptedTransport(
                [response(200, {"items": ["not-an-operation"]})]
            ),
        )
        with pytest.raises(RegistryError, match="invalid operation list"):
            find(client)

    invalid_created = HTTPRegistryClient(
        "https://registry.example.test",
        access_jwt="jwt",
        transport=ScriptedTransport(
            [response(200, resource_payload()), response(201, {"id": 1})]
        ),
    )
    with pytest.raises(RegistryError, match="invalid operation"):
        invalid_created.create_operation(plan)
    unreadable_created = HTTPRegistryClient(
        "https://registry.example.test",
        access_jwt="jwt",
        transport=ScriptedTransport(
            [
                response(200, resource_payload()),
                response(201, {"id": "op-1"}),
                response(404, {"error": {"code": "not_found"}}),
            ]
        ),
    )
    with pytest.raises(RegistryError, match="read back"):
        unreadable_created.create_operation(plan)

    invalid_lock = HTTPRegistryClient(
        "https://registry.example.test",
        access_jwt="jwt",
        transport=ScriptedTransport(
            [response(201, {"items": [{"scope": 1, "fencingToken": "bad"}]})]
        ),
    )
    with pytest.raises(RegistryError, match="invalid lock lease"):
        invalid_lock.acquire_locks("op", ["resource/r"])

    invalid_scope = operation_payload(plan)
    invalid_scope["resources"][0]["resourceKey"] = 1
    renew = HTTPRegistryClient(
        "https://registry.example.test",
        access_jwt="jwt",
        transport=ScriptedTransport([response(200, invalid_scope)]),
    )
    with pytest.raises(RegistryError, match="resource scope"):
        renew.renew_locks("op-1")

    authority = RegistryAuthority(
        operationId="op-1",
        lockScope="resource/host-web01",
        fencingToken=1,
        operationRevision=1,
        resourceRevision=1,
    )
    missing_step = operation_payload(plan)
    missing_step["steps"] = []
    evidence = HostOperationEvidence(
        apiVersion="atlas.host-operation/v1",
        kind="HostOperationEvidence",
        operationId="op-1",
        planId=plan.metadata.plan_id,
        resourceId=plan.resource.id,
        phase="allocate",
        status="failed",
        startedAt=datetime.now(UTC),
        finishedAt=datetime.now(UTC),
        attempt=1,
    )
    record = HTTPRegistryClient(
        "https://registry.example.test",
        access_jwt="jwt",
        transport=ScriptedTransport([response(200, missing_step)]),
    )
    with pytest.raises(RegistryConflictError, match="step is missing"):
        record.record_step("op-1", evidence, authority)

    for payload, message in (
        (response(404, {"error": {"code": "not_found"}}), "identity is missing"),
        (
            response(200, resource_payload()["resource"] | {"name": "other"}),
            "name does not match",
        ),
        (
            response(200, resource_payload(lifecycle="retired")),
            "cannot be provisioned",
        ),
    ):
        reserve = HTTPRegistryClient(
            "https://registry.example.test",
            access_jwt="jwt",
            transport=ScriptedTransport([payload]),
        )
        with pytest.raises(RegistryConflictError, match=message):
            reserve.reserve_resource("op-1", plan.resource, authority)

    stale_reservation = HTTPRegistryClient(
        "https://registry.example.test",
        access_jwt="jwt",
        transport=ScriptedTransport([response(200, resource_payload(revision=2))]),
    )
    with pytest.raises(RegistryConflictError, match="revision changed"):
        stale_reservation.reserve_resource("op-1", plan.resource, authority)

    no_resources = operation_payload(plan)
    no_resources["resources"] = []
    authority_client = HTTPRegistryClient(
        "https://registry.example.test",
        access_jwt="jwt",
        transport=ScriptedTransport(
            [
                response(201, {"items": [{"scope": "resource/r", "fencingToken": 1}]}),
                response(200, no_resources),
            ]
        ),
    )
    with pytest.raises(RegistryConflictError, match="no Resource"):
        authority_client.acquire_locks("op-1", ["resource/r"])

    with pytest.raises(RegistryError, match="operation"):
        _operation_model({"operation": [], "resources": [], "steps": []})
    with pytest.raises(RegistryError, match="operation"):
        _operation_model({"id": "missing-fields"})

    bad_http = HTTPRegistryClient(
        "https://registry.example.test",
        access_jwt="jwt",
        transport=ScriptedTransport([HTTPResponse(500, b"not-json")]),
    )
    with pytest.raises(RegistryError, match="unknown"):
        bad_http.get_operation("op")

    registry = InMemoryRegistryClient(list(fixture.registry.resources.values()))
    operation = registry.create_operation(plan)
    with pytest.raises(RegistryConflictError, match="known lock"):
        registry.renew_locks(operation.id)
    auth = registry.acquire_locks(operation.id, ["resource/host-web01"])
    registry.start_operation(operation.id, auth)
    assert registry.start_operation(operation.id, auth).status == "running"
    with pytest.raises(RegistryConflictError, match="operation mismatch"):
        registry.start_operation(
            operation.id,
            auth.model_copy(update={"operation_id": "other"}),
        )
    missing_phase = evidence.model_copy(update={"phase": ProvisioningPhase.ACTIVATE})
    operation_now = registry.operations[operation.id]
    registry.operations[operation.id] = operation_now.model_copy(update={"steps": []})
    with pytest.raises(RegistryConflictError, match="step is missing"):
        registry.record_step(operation.id, missing_phase, auth)
    registry.operations[operation.id] = operation_now
    resource = registry.resources[plan.resource.id]
    registry.resources[plan.resource.id] = resource.model_copy(
        update={"lifecycle_state": "retired"}
    )
    with pytest.raises(RegistryConflictError, match="lifecycle collision"):
        registry.reserve_resource(operation.id, plan.resource, auth)
    registry.resources[plan.resource.id] = resource.model_copy(update={"revision": 2})
    with pytest.raises(RegistryConflictError, match="revision changed"):
        registry.reserve_resource(operation.id, plan.resource, auth)
    with pytest.raises(RegistryConflictError, match="operation not found"):
        registry._required_operation("missing")
    with pytest.raises(RegistryConflictError, match="Resource not found"):
        registry._required_resource("missing")
    terminal = registry.operations[operation.id].model_copy(
        update={"status": "completed"}
    )
    registry.operations[operation.id] = terminal
    with pytest.raises(RegistryConflictError, match="not lockable"):
        registry.acquire_locks(operation.id, ["resource/host-web01"])


def test_transport_timeout_helpers_and_readiness_optional_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = urllib.error.HTTPError(
        "https://example.test",
        409,
        "conflict",
        {},
        io.BytesIO(b"{}"),
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: (_ for _ in ()).throw(error),
    )
    assert (
        UrllibTransport().request("GET", "https://example.test", {}, None, 1).status
        == 409
    )
    assert _timeout_text(b"bytes") == "bytes"
    assert _timeout_text("text") == "text"

    fixture = make_host_fixture(tmp_path)
    plan_data = fixture.plan().as_artifact()
    plan_data["readiness"]["require_guest_agent"] = False
    plan_data["readiness"]["require_cloud_init"] = False
    plan_data["metadata"].pop("fingerprint")
    plan = set_fingerprint(HostOperationPlan.model_validate(plan_data))
    checker = HostReadinessChecker(
        connector=lambda *_args, **_kwargs: object(),
        runner=controller_module.SubprocessRunner(),
    )
    # Avoid a real ssh call while exercising an object without close().
    checker._runner = type(
        "Runner",
        (),
        {"run": lambda self, *args, **kwargs: type("R", (), {"return_code": 0})()},
    )()
    result = checker.wait(
        HostContext(plan),
            ProviderObservation(
                exists=True,
                running=True,
                guestAgentReady=False,
                addresses=[plan.readiness.address],
            ),
        )
    assert result.status == "passed"


def test_registry_status_mapping_and_default_exception_code(tmp_path: Path) -> None:
    assert _registry_step_status(StepStatus.PENDING) == "planned"
    assert _registry_step_status(StepStatus.RUNNING) == "running"
    assert _registry_step_status(StepStatus.FAILED) == "failed"
    assert _registry_step_status(StepStatus.SKIPPED) == "skipped"
    assert _registry_step_status(StepStatus.NEEDS_RECONCILE) == "blocked"
    assert _registry_step_status(StepStatus.ROLLED_BACK) == "succeeded"
    assert controller_module._exception_exit_code(HostOperationError()) == 1


def test_completed_activation_and_terminal_evidence_fallbacks(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    controller = _inline_controller(fixture)
    assert controller.apply(plan, confirm=plan.metadata.plan_id).exit_code == 0
    operation_id = next(iter(fixture.registry.operations))
    operation = fixture.registry.operations[operation_id]
    fixture.registry.operations[operation_id] = operation.model_copy(
        update={"status": OperationStatus.NEEDS_RECONCILE.value}
    )
    fixture.registry.acquire_locks(operation_id, ["resource/host-web01"])

    revalidated = execute_phase(
        ProvisioningPhase.ACTIVATE,
        plan,
        operation_id,
        registry=fixture.registry,
        provider=fixture.provider,
        configurator=fixture.configurator,
        readiness=fixture.readiness,
        resume=True,
    )
    assert revalidated.exit_code == 0
    assert revalidated.evidence.status is StepStatus.SKIPPED

    operation = fixture.registry.operations[operation_id]
    activate_index = next(
        index
        for index, step in enumerate(operation.steps)
        if step.name == ProvisioningPhase.ACTIVATE.value
    )
    invalid_steps = list(operation.steps)
    invalid_steps[activate_index] = invalid_steps[activate_index].model_copy(
        update={"evidence": {"invalid": True}}
    )
    invalid = _terminal_evidence(
        plan,
        operation.model_copy(update={"steps": invalid_steps}),
    )
    assert invalid.evidence.message == "operation was already complete"

    missing_steps = list(operation.steps)
    missing_steps[activate_index] = missing_steps[activate_index].model_copy(
        update={"evidence": None}
    )
    missing = _terminal_evidence(
        plan,
        operation.model_copy(update={"steps": missing_steps}),
    )
    assert missing.evidence.message == "operation was already complete"


def test_remaining_http_registry_control_flow(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()

    malformed_match = _client(
        ScriptedTransport(
            [
                response(
                    200,
                    {
                        "items": [
                            {
                                "id": 1,
                                "plan": {"intent": {"idempotencyKey": "matching-key"}},
                            }
                        ]
                    },
                )
            ]
        )
    )
    with pytest.raises(RegistryError, match="invalid operation"):
        malformed_match.find_operation_by_idempotency_key("matching-key")

    invalid_resource_list = _client(ScriptedTransport([response(200, {"items": {}})]))
    with pytest.raises(RegistryError, match="operation list"):
        invalid_resource_list.find_operation_for_resource("host-web01")

    cached_renew_transport = ScriptedTransport(
        [
            response(
                201,
                {
                    "items": [
                        {
                            "scope": "resource/host-web01",
                            "fencingToken": 7,
                        }
                    ]
                },
            ),
            response(200, operation_payload(plan)),
            response(200, resource_payload()),
        ]
    )
    cached_renew = _client(cached_renew_transport)
    cached_renew._lock_scopes["op-1"] = ["resource/host-web01"]
    assert cached_renew.renew_locks("op-1").fencing_token == 7
    assert cached_renew_transport.calls[0]["body"]["scopes"] == ["resource/host-web01"]

    invalid_authority_detail = operation_payload(plan)
    invalid_authority_detail["resources"][0]["resourceKey"] = 1
    invalid_authority = _client(
        ScriptedTransport(
            [
                response(
                    201,
                    {"items": [{"scope": "resource/host-web01", "fencingToken": 1}]},
                ),
                response(200, invalid_authority_detail),
            ]
        )
    )
    with pytest.raises(RegistryError, match="Resource is invalid"):
        invalid_authority.acquire_locks("op-1", ["resource/host-web01"])

    authority = RegistryAuthority(
        operationId="op-1",
        lockScope="resource/host-web01",
        fencingToken=1,
        operationRevision=1,
        resourceRevision=1,
    )
    missing_operation = _client(
        ScriptedTransport([response(404, {"error": {"code": "not_found"}})])
    )
    with pytest.raises(RegistryConflictError, match="operation not found"):
        missing_operation.start_operation("op-1", authority)

    missing_resource = _client(
        ScriptedTransport([response(404, {"error": {"code": "not_found"}})])
    )
    with pytest.raises(RegistryConflictError, match="Resource not found"):
        missing_resource.bind_provider(
            "op-1",
            "host-web01",
            ProviderEvidence(
                provider="proxmox",
                resourceType="vm",
                resourceId="qemu/121",
                resourceName="web01",
                locator={},
                ownershipMarker=True,
            ),
            authority,
        )

    unknown_error = _client(ScriptedTransport([response(500, {"error": {"code": 1}})]))
    with pytest.raises(RegistryError, match=r"HTTP 500 \(unknown\)"):
        unknown_error.get_operation("op-1")


def test_remaining_in_memory_and_subprocess_control_flow(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    registry = InMemoryRegistryClient(list(fixture.registry.resources.values()))
    operation = registry.create_operation(plan)
    assert registry.find_operation_for_resource("missing") is None
    authority = registry.acquire_locks(operation.id, ["resource/host-web01"])
    operation = registry.start_operation(operation.id, authority)
    allocation_index = next(
        index
        for index, step in enumerate(operation.steps)
        if step.name == ProvisioningPhase.ALLOCATE.value
    )
    steps = list(operation.steps)
    steps[allocation_index] = steps[allocation_index].model_copy(
        update={"status": StepStatus.ROLLED_BACK.value}
    )
    registry.operations[operation.id] = operation.model_copy(
        update={
            "status": OperationStatus.ROLLING_BACK.value,
            "steps": steps,
        }
    )
    rolled_back = registry.cancel_operation(operation.id, authority)
    assert rolled_back.status == OperationStatus.ROLLED_BACK.value

    child = SubprocessRunner().run([sys.executable, "-c", "pass"])
    assert child.return_code == 0
    assert child.stderr == ""
