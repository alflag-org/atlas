from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
from atlas_host_operations.artifacts import set_fingerprint
from atlas_host_operations.configurators import FakeHostConfigurator
from atlas_host_operations.controller import (
    EXECUTION_PHASES,
    HostController,
    InlinePhaseExecutor,
    PhaseExecution,
    SubprocessPhaseExecutor,
    _exception_exit_code,
    _host_lifecycle,
    _host_operation_status,
    _RegistryAuthorityLease,
    build_host_plan,
    configurator_for_plan,
    execute_phase,
    phase_job_main,
    provider_for_plan,
    reconcile_job_main,
)
from atlas_host_operations.errors import (
    AdapterError,
    HostOperationError,
    InputError,
    PlanError,
    RegistryAuthenticationError,
    RegistryConflictError,
    RegistryError,
    SafetyError,
    UnknownProviderResult,
)
from atlas_host_operations.lifecycle import (
    OperationStatus,
    ProvisioningPhase,
    ResourceLifecycle,
    StepStatus,
)
from atlas_host_operations.models import (
    HostOperationEvidence,
    HostOperationPlan,
    ProviderObservation,
)
from atlas_host_operations.providers import FakeCloudProvider
from atlas_host_operations.registry import InMemoryRegistryClient
from atlas_host_operations.subprocesses import ChildResult, RecordingRunner

from atlas.manifests import load_manifest

from .test_host_operations_support import make_host_fixture


def _controller(fixture):
    executor = InlinePhaseExecutor(
        fixture.registry,
        fixture.provider,
        fixture.configurator,
        fixture.readiness,
    )
    return HostController(
        fixture.registry,
        executor,
        provider=fixture.provider,
        configurator=fixture.configurator,
        readiness=fixture.readiness,
    )


def _write_plan(path: Path, plan: HostOperationPlan) -> Path:
    path.write_text(json.dumps(plan.as_artifact()), encoding="utf-8")
    return path


def _refingerprint(plan: HostOperationPlan, update) -> HostOperationPlan:
    data = copy.deepcopy(plan.as_artifact())
    update(data)
    data["metadata"].pop("fingerprint", None)
    return set_fingerprint(HostOperationPlan.model_validate(data))


def test_infrastructure_manifest_exposes_controllers_and_private_jobs() -> None:
    manifest = load_manifest(Path("infrastructure-operations"))
    assert manifest.name == "infrastructure-operations"
    assert list(manifest.commands) == [
        "hostctl",
        "imagectl",
        "providerctl",
        "operationctl",
    ]
    assert len(manifest.jobs) == 23
    assert manifest.jobs["host-provider-allocate"].default_timeout_seconds == 1800


def test_full_fake_lifecycle_order_status_verify_and_idempotency(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    controller = _controller(fixture)
    plan_path = _write_plan(tmp_path / "plan.json", plan)
    planned = controller.status(str(plan_path))
    assert planned.operation_status == "planned"
    assert planned.operation_id is None

    result = controller.apply(plan, confirm=plan.metadata.plan_id)
    assert result.exit_code == 0
    assert result.evidence.phase is ProvisioningPhase.ACTIVATE
    operation_id = next(iter(fixture.registry.operations))
    status = controller.status(operation_id)
    assert status.operation_status == OperationStatus.COMPLETED.value
    assert status.resource_lifecycle == ResourceLifecycle.ACTIVE.value
    assert status.current_phase is None
    assert all(step["status"] == "succeeded" for step in status.steps)
    provider_verifications = fixture.provider.calls.count("verify")
    verification = controller.verify(operation_id)
    assert verification.status == "passed"
    assert fixture.provider.calls.count("verify") == provider_verifications + 1

    repeated = controller.apply(plan, confirm=plan.metadata.plan_id)
    assert repeated.exit_code == 0
    assert repeated.evidence.phase is ProvisioningPhase.ACTIVATE
    assert fixture.provider.calls.count("allocate") == 1

    calls = fixture.registry.call_log
    assert calls.index("start-operation") < calls.index(
        "record-step:validate:running"
    )
    assert calls.index("record-step:validate:succeeded") < calls.index(
        "reserve-resource"
    )
    assert calls.index("reserve-resource") < calls.index("lifecycle:allocated")
    assert calls.index("lifecycle:allocated") < calls.index("bind-provider")
    assert calls.index("bind-provider") < calls.index("lifecycle:bootstrapped")
    assert calls.index("lifecycle:configured") < calls.index("lifecycle:ready")
    assert calls.index("lifecycle:ready") < calls.index("complete-operation")


def test_confirmation_source_and_plan_age_safety(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    controller = _controller(fixture)
    with pytest.raises(SafetyError, match="exactly match"):
        controller.apply(plan, confirm="wrong")
    stale = _refingerprint(
        plan,
        lambda data: data["metadata"].update(
            createdAt=(datetime.now(UTC) - timedelta(hours=1)).isoformat()
        ),
    )
    with pytest.raises(PlanError, match="stale"):
        controller.apply(stale, confirm=stale.metadata.plan_id)
    future = _refingerprint(
        plan,
        lambda data: data["metadata"].update(
            createdAt=(datetime.now(UTC) + timedelta(hours=1)).isoformat()
        ),
    )
    with pytest.raises(PlanError, match="future"):
        controller.apply(future, confirm=future.metadata.plan_id)
    fixture.provider_definition.write_text("changed: true\n", encoding="utf-8")
    with pytest.raises(PlanError, match="provider definition changed"):
        controller.apply(plan, confirm=plan.metadata.plan_id)


def test_build_plan_rejects_registry_inventory_and_adapter_mismatches(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    missing = InMemoryRegistryClient()
    with pytest.raises(InputError, match="reserved"):
        build_host_plan(
            fixture.host_spec,
            registry=missing,
            provider=fixture.provider,
            configurator=fixture.configurator,
        )
    resource = fixture.registry.resources["host-web01"]
    fixture.registry.resources["host-web01"] = resource.model_copy(
        update={"name": "other"}
    )
    with pytest.raises(InputError, match="collides"):
        fixture.plan()
    fixture.registry.resources["host-web01"] = resource.model_copy(
        update={"kind": "volume"}
    )
    with pytest.raises(InputError, match="kind"):
        fixture.plan()
    fixture.registry.resources["host-web01"] = resource.model_copy(
        update={"lifecycle_state": "ready"}
    )
    with pytest.raises(InputError, match="unbound absent"):
        fixture.plan()
    fixture.registry.resources["host-web01"] = resource.model_copy(
        update={"binding": {"providerId": "p"}}
    )
    with pytest.raises(InputError, match="unbound absent"):
        fixture.plan()
    fixture.registry.resources["host-web01"] = resource
    (fixture.project / "inventories" / "site01" / "hosts.yml").write_text(
        "all:\n  hosts:\n    other: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(InputError, match="absent from inventory"):
        fixture.plan()


def test_build_plan_rejects_failed_or_misnamed_adapters(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    wrong_provider = FakeCloudProvider(name="other")
    with pytest.raises(InputError, match="provider adapter name"):
        build_host_plan(
            fixture.host_spec,
            registry=fixture.registry,
            provider=wrong_provider,
            configurator=fixture.configurator,
        )
    wrong_configurator = FakeHostConfigurator(name="other")
    with pytest.raises(InputError, match="configuration adapter name"):
        build_host_plan(
            fixture.host_spec,
            registry=fixture.registry,
            provider=fixture.provider,
            configurator=wrong_configurator,
        )
    broken = FakeCloudProvider(name="fake-cloud", fail_on="validate")
    with pytest.raises(AdapterError, match="validate failed"):
        build_host_plan(
            fixture.host_spec,
            registry=fixture.registry,
            provider=broken,
            configurator=fixture.configurator,
        )


def test_resume_revalidates_completed_steps_and_recovers_configuration_failure(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    fixture.configurator.fail_on = "converge"
    plan = fixture.plan()
    controller = _controller(fixture)
    failed = controller.apply(plan, confirm=plan.metadata.plan_id)
    assert failed.exit_code == 4
    assert failed.evidence.phase is ProvisioningPhase.CONVERGE
    operation_id = next(iter(fixture.registry.operations))
    assert controller.status(operation_id).operation_status == "needs-reconcile"
    assert fixture.provider.exists

    fixture.configurator.fail_on = None
    resumed = controller.resume(plan, confirm=plan.metadata.plan_id)
    assert resumed.exit_code == 0
    assert controller.status(operation_id).operation_status == "completed"
    assert fixture.provider.calls.count("allocate") == 1
    assert "observe" in fixture.provider.calls


@pytest.mark.parametrize(
    ("failure", "phase"),
    [
        ("bootstrap", ProvisioningPhase.BOOTSTRAP),
        ("converge", ProvisioningPhase.CONVERGE),
        ("verify", ProvisioningPhase.CONFIGURATION_VERIFY),
    ],
)
def test_configuration_failures_retain_the_provider_resource(
    tmp_path: Path,
    failure: str,
    phase: ProvisioningPhase,
) -> None:
    fixture = make_host_fixture(tmp_path)
    fixture.configurator.fail_on = failure
    plan = fixture.plan()
    plan_path = _write_plan(tmp_path / "plan.json", plan)
    controller = _controller(fixture)
    result = controller.apply(plan, confirm=plan.metadata.plan_id)
    assert result.exit_code == 4
    assert result.evidence.phase is phase
    assert result.evidence.status is StepStatus.NEEDS_RECONCILE
    assert fixture.provider.exists
    assert "rollback" not in fixture.provider.calls
    rollback = controller.rollback(str(plan_path), confirm=plan.metadata.plan_id)
    assert rollback.exit_code == 6
    assert fixture.provider.exists
    assert "rollback" not in fixture.provider.calls


@pytest.mark.parametrize("phase", EXECUTION_PHASES)
@pytest.mark.parametrize("boundary", ["before", "after"])
def test_resume_after_process_crash_at_each_private_phase_boundary(
    tmp_path: Path,
    phase: ProvisioningPhase,
    boundary: str,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    inline = InlinePhaseExecutor(
        fixture.registry,
        fixture.provider,
        fixture.configurator,
        fixture.readiness,
    )

    class CrashingExecutor:
        def execute(self, current, current_plan, operation_id, *, resume):
            if current is phase and boundary == "before":
                raise RuntimeError("simulated process crash")
            result = inline.execute(
                current,
                current_plan,
                operation_id,
                resume=resume,
            )
            if current is phase and boundary == "after":
                raise RuntimeError("simulated process crash")
            return result

    interrupted = HostController(
        fixture.registry,
        CrashingExecutor(),
        provider=fixture.provider,
        configurator=fixture.configurator,
        readiness=fixture.readiness,
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        interrupted.apply(plan, confirm=plan.metadata.plan_id)

    resumed = _controller(fixture).resume(plan, confirm=plan.metadata.plan_id)
    assert resumed.exit_code == 0
    operation_id = next(iter(fixture.registry.operations))
    assert fixture.registry.operations[operation_id].status == OperationStatus.COMPLETED


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_resume_after_process_crash_at_validate_boundary(
    tmp_path: Path,
    boundary: str,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()

    class CrashingValidationController(HostController):
        def _record_plan_validation(self, current_plan, operation):
            if boundary == "before":
                raise RuntimeError("simulated validation crash")
            super()._record_plan_validation(current_plan, operation)
            raise RuntimeError("simulated validation crash")

    interrupted = CrashingValidationController(
        fixture.registry,
        InlinePhaseExecutor(
            fixture.registry,
            fixture.provider,
            fixture.configurator,
            fixture.readiness,
        ),
    )
    with pytest.raises(RuntimeError, match="simulated validation crash"):
        interrupted.apply(plan, confirm=plan.metadata.plan_id)

    resumed = _controller(fixture).resume(plan, confirm=plan.metadata.plan_id)
    assert resumed.exit_code == 0


def test_unknown_allocation_requires_reconcile_then_resume(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    fixture.provider.unknown_allocate = True
    plan = fixture.plan()
    controller = _controller(fixture)
    result = controller.apply(plan, confirm=plan.metadata.plan_id)
    assert result.exit_code == 6
    assert result.evidence.status is StepStatus.NEEDS_RECONCILE
    fixture.provider.unknown_allocate = False
    assert controller.resume(plan, confirm=plan.metadata.plan_id).exit_code == 0
    assert fixture.provider.calls.count("allocate") == 2
    assert fixture.provider.calls.index("observe") < fixture.provider.calls.index(
        "allocate", 2
    )


def test_rollback_does_not_cancel_an_unknown_allocation(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    fixture.provider.unknown_allocate = True
    plan = fixture.plan()
    plan_path = _write_plan(tmp_path / "plan.json", plan)
    controller = _controller(fixture)
    assert controller.apply(plan, confirm=plan.metadata.plan_id).exit_code == 6

    result = controller.rollback(str(plan_path), confirm=plan.metadata.plan_id)

    assert result.exit_code == 6
    assert result.evidence.phase is ProvisioningPhase.ALLOCATE
    assert "rollback" not in fixture.provider.calls


def test_resume_observes_a_running_allocation_after_process_loss(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    controller = _controller(fixture)
    allocate = fixture.provider.allocate

    def crash_after_allocation(context, authority):
        allocate(context, authority)
        raise RuntimeError("allocation response was lost")

    fixture.provider.allocate = crash_after_allocation
    with pytest.raises(RuntimeError, match="response was lost"):
        controller.apply(plan, confirm=plan.metadata.plan_id)
    fixture.provider.allocate = allocate

    assert controller.resume(plan, confirm=plan.metadata.plan_id).exit_code == 0
    assert fixture.provider.calls.count("allocate") == 1
    assert "observe" in fixture.provider.calls


def test_resume_does_not_restart_a_running_registry_step(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    controller = _controller(fixture)
    wait = fixture.readiness.wait

    def interrupt_wait(context, observation):
        raise RuntimeError("readiness process was interrupted")

    fixture.readiness.wait = interrupt_wait
    with pytest.raises(RuntimeError, match="readiness process was interrupted"):
        controller.apply(plan, confirm=plan.metadata.plan_id)
    fixture.readiness.wait = wait

    assert controller.resume(plan, confirm=plan.metadata.plan_id).exit_code == 0


def test_resume_completes_after_activate_evidence_response_window(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    controller = _controller(fixture)
    complete_operation = fixture.registry.complete_operation

    def interrupt_before_complete(operation_id, authority):
        raise RuntimeError("completion process was interrupted")

    fixture.registry.complete_operation = interrupt_before_complete
    with pytest.raises(RuntimeError, match="completion process was interrupted"):
        controller.apply(plan, confirm=plan.metadata.plan_id)
    fixture.registry.complete_operation = complete_operation

    operation_id = next(iter(fixture.registry.operations))
    interrupted = fixture.registry.operations[operation_id]
    assert interrupted.status == OperationStatus.RUNNING.value
    assert interrupted.steps[-1].status == StepStatus.SUCCEEDED.value
    assert controller.resume(plan, confirm=plan.metadata.plan_id).exit_code == 0
    assert fixture.registry.operations[operation_id].status == OperationStatus.COMPLETED


def test_unknown_allocation_recovers_live_resource_without_reallocation(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    fixture.provider.unknown_allocate = True
    plan = fixture.plan()
    controller = _controller(fixture)
    assert controller.apply(plan, confirm=plan.metadata.plan_id).exit_code == 6
    fixture.provider.unknown_allocate = False
    fixture.provider.exists = True
    fixture.provider.running = True
    fixture.provider.ownership_marker = True

    resumed = controller.resume(plan, confirm=plan.metadata.plan_id)
    assert resumed.exit_code == 0
    assert fixture.provider.calls.count("allocate") == 1
    operation_id = next(iter(fixture.registry.operations))
    allocation = next(
        step
        for step in fixture.registry.operations[operation_id].steps
        if step.name == ProvisioningPhase.ALLOCATE.value
    )
    assert allocation.evidence["message"] == (
        "allocation recovered from live provider state"
    )


def test_unknown_allocation_stays_blocked_without_certain_live_evidence(
    tmp_path: Path,
) -> None:
    owned = make_host_fixture(tmp_path / "unowned")
    owned.provider.unknown_allocate = True
    owned_plan = owned.plan()
    owned_controller = _controller(owned)
    owned_controller.apply(owned_plan, confirm=owned_plan.metadata.plan_id)
    owned.provider.unknown_allocate = False
    owned.provider.exists = True
    owned.provider.running = True
    without_ownership = owned_controller.resume(
        owned_plan,
        confirm=owned_plan.metadata.plan_id,
    )
    assert without_ownership.exit_code == 6
    assert "ownership evidence" in without_ownership.evidence.message

    uncertain = make_host_fixture(tmp_path / "uncertain")
    uncertain.provider.unknown_allocate = True
    uncertain_plan = uncertain.plan()
    uncertain_controller = _controller(uncertain)
    uncertain_controller.apply(
        uncertain_plan,
        confirm=uncertain_plan.metadata.plan_id,
    )
    uncertain.provider.unknown_allocate = False
    uncertain.provider.observe = lambda context: ProviderObservation(
        exists=False,
        running=False,
        guestAgentReady=False,
        absenceConfirmed=False,
    )
    unresolved = uncertain_controller.resume(
        uncertain_plan,
        confirm=uncertain_plan.metadata.plan_id,
    )
    assert unresolved.exit_code == 6
    assert "absence is not confirmed" in unresolved.evidence.message


def test_apply_refuses_reconcile_and_resume_refuses_missing_or_terminal_operation(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    fixture.provider.unknown_allocate = True
    plan = fixture.plan()
    controller = _controller(fixture)
    controller.apply(plan, confirm=plan.metadata.plan_id)
    with pytest.raises(SafetyError, match="use hostctl resume"):
        controller.apply(plan, confirm=plan.metadata.plan_id)
    other = _refingerprint(
        plan,
        lambda data: data["metadata"].update(
            planId="plan-other",
            idempotencyKey="other",
        ),
    )
    with pytest.raises(PlanError, match="does not exist"):
        controller.resume(other, confirm=other.metadata.plan_id)

    operation_id = next(iter(fixture.registry.operations))
    operation = fixture.registry.operations[operation_id]
    for terminal in ("failed", "cancelled", "rolled-back"):
        fixture.registry.operations[operation_id] = operation.model_copy(
            update={"status": terminal}
        )
        with pytest.raises(SafetyError, match="terminal"):
            controller.resume(plan, confirm=plan.metadata.plan_id)
    fixture.registry.operations[operation_id] = operation.model_copy(
        update={"status": "failed"}
    )
    with pytest.raises(SafetyError, match="terminal"):
        controller.apply(plan, confirm=plan.metadata.plan_id)


def test_idempotency_collision_is_rejected(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    controller = _controller(fixture)
    operation = fixture.registry.create_operation(plan)
    different = _refingerprint(
        plan,
        lambda data: data["metadata"].update(planId="different"),
    )
    operation.plan["intent"]["hostPlan"] = different.as_artifact()
    with pytest.raises(RegistryConflictError, match="different plan"):
        controller.apply(plan, confirm=plan.metadata.plan_id)


def test_corrective_source_change_gets_a_new_idempotency_key(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    original = fixture.plan()
    fixture.provider_input.write_text("name: web01\ncores: 4\n", encoding="utf-8")
    corrected = fixture.plan()
    assert corrected.metadata.idempotency_key != original.metadata.idempotency_key


def test_rollback_before_allocation_after_binding_and_after_configuration(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    plan_path = _write_plan(tmp_path / "plan.json", plan)
    operation = fixture.registry.create_operation(plan)
    controller = _controller(fixture)
    before = controller.rollback(str(plan_path), confirm=plan.metadata.plan_id)
    assert before.exit_code == 0
    assert before.evidence.status is StepStatus.ROLLED_BACK
    assert fixture.registry.operations[operation.id].status == "cancelled"

    second = make_host_fixture(tmp_path / "second")
    second.readiness.status = "failed"
    second_plan = second.plan()
    second_path = _write_plan(second.root / "plan.json", second_plan)
    second_controller = _controller(second)
    stopped = second_controller.apply(second_plan, confirm=second_plan.metadata.plan_id)
    assert stopped.evidence.phase is ProvisioningPhase.WAIT_READY
    second_operation = next(iter(second.registry.operations.values()))
    second.registry.operations[second_operation.id] = second_operation.model_copy(
        update={
            "steps": [
                step.model_copy(update={"status": "running"})
                if step.name == ProvisioningPhase.BIND.value
                else step
                for step in second_operation.steps
            ]
        }
    )
    rolled_back = second_controller.rollback(
        str(second_path),
        confirm=second_plan.metadata.plan_id,
    )
    assert rolled_back.exit_code == 0
    assert not second.provider.exists
    assert second.registry.get_resource("host-web01").binding is None

    third = make_host_fixture(tmp_path / "third")
    third.configurator.fail_on = "converge"
    third_plan = third.plan()
    third_path = _write_plan(third.root / "plan.json", third_plan)
    third_controller = _controller(third)
    third_controller.apply(third_plan, confirm=third_plan.metadata.plan_id)
    retained = third_controller.rollback(
        str(third_path),
        confirm=third_plan.metadata.plan_id,
    )
    assert retained.exit_code == 6
    assert third.provider.exists


def test_rollback_refuses_active_host_and_reports_provider_failure(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    path = _write_plan(tmp_path / "plan.json", plan)
    controller = _controller(fixture)
    controller.apply(plan, confirm=plan.metadata.plan_id)
    with pytest.raises(SafetyError, match="HostRetire"):
        controller.rollback(str(path), confirm=plan.metadata.plan_id)
    fixture.registry.resources.pop(plan.resource.id)
    with pytest.raises(SafetyError, match="HostRetire"):
        controller.rollback(str(path), confirm=plan.metadata.plan_id)

    failed = make_host_fixture(tmp_path / "failed")
    failed.readiness.status = "failed"
    failed_plan = failed.plan()
    failed_path = _write_plan(failed.root / "plan.json", failed_plan)
    failed_controller = _controller(failed)
    failed_controller.apply(failed_plan, confirm=failed_plan.metadata.plan_id)
    failed.provider.ownership_marker = False
    result = failed_controller.rollback(
        str(failed_path), confirm=failed_plan.metadata.plan_id
    )
    assert result.exit_code == 1
    assert failed.registry.get_resource("host-web01").binding is not None


@pytest.mark.parametrize(
    ("error", "exit_code"),
    [
        (AdapterError("rollback adapter failed"), 4),
        (UnknownProviderResult("rollback result is unknown"), 6),
    ],
)
def test_rollback_adapter_errors_retain_binding_and_require_reconciliation(
    tmp_path: Path,
    error: Exception,
    exit_code: int,
) -> None:
    fixture = make_host_fixture(tmp_path)
    fixture.readiness.status = "failed"
    plan = fixture.plan()
    path = _write_plan(tmp_path / "plan.json", plan)
    controller = _controller(fixture)
    controller.apply(plan, confirm=plan.metadata.plan_id)

    def fail_rollback(context, evidence, authority):
        raise error

    fixture.provider.rollback = fail_rollback
    result = controller.rollback(str(path), confirm=plan.metadata.plan_id)
    assert result.exit_code == exit_code
    assert result.evidence.status is StepStatus.NEEDS_RECONCILE
    assert fixture.registry.get_resource("host-web01").binding is not None


def test_rollback_refuses_a_different_registry_binding(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    fixture.readiness.status = "failed"
    plan = fixture.plan()
    path = _write_plan(tmp_path / "plan.json", plan)
    controller = _controller(fixture)
    controller.apply(plan, confirm=plan.metadata.plan_id)
    resource = fixture.registry.resources[plan.resource.id]
    fixture.registry.resources[plan.resource.id] = resource.model_copy(
        update={
            "binding": {
                **resource.binding,
                "providerResourceId": "fake/other",
            }
        }
    )

    result = controller.rollback(str(path), confirm=plan.metadata.plan_id)

    assert result.exit_code == 6
    assert fixture.provider.exists
    assert "rollback" not in fixture.provider.calls


def test_status_and_verify_target_errors(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    controller = _controller(fixture)
    with pytest.raises(PlanError, match="not found"):
        controller.status("missing")
    plan = fixture.plan()
    plan_path = _write_plan(tmp_path / "plan.json", plan)
    with pytest.raises(PlanError, match="no Global Registry"):
        controller.verify(str(plan_path))


def test_subprocess_phase_executor_exact_job_argv_and_invalid_evidence(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    evidence = HostOperationEvidence(
        apiVersion="atlas.host-operation/v1",
        kind="HostOperationEvidence",
        operationId="op-1",
        planId=plan.metadata.plan_id,
        resourceId=plan.resource.id,
        phase="reserve",
        status="succeeded",
        startedAt=datetime.now(UTC),
        finishedAt=datetime.now(UTC),
        attempt=1,
    )
    runner = RecordingRunner([ChildResult((), 0, json.dumps(evidence.as_artifact()))])
    executor = SubprocessPhaseExecutor(runner, atlas_executable="/atlas")
    result = executor.execute(
        ProvisioningPhase.RESERVE,
        plan,
        "op-1",
        resume=True,
    )
    assert result.exit_code == 0
    assert runner.calls[0]["argv"] == [
        "/atlas",
        "job",
        "run",
        "infrastructure-operations",
        "host-registry-reserve",
        "--",
        "--plan",
        "-",
        "--operation",
        "op-1",
        "--resume",
    ]
    assert runner.calls[0]["env"]["ATLAS_OPERATION_ID"] == "op-1"
    with pytest.raises(AdapterError, match="invalid evidence"):
        SubprocessPhaseExecutor(
            RecordingRunner([ChildResult((), 0, "invalid")])
        ).execute(ProvisioningPhase.RESERVE, plan, "op-1", resume=False)

    mismatched = evidence.model_copy(update={"operation_id": "op-other"})
    with pytest.raises(AdapterError, match="mismatched evidence"):
        SubprocessPhaseExecutor(
            RecordingRunner([ChildResult((), 0, json.dumps(mismatched.as_artifact()))])
        ).execute(ProvisioningPhase.RESERVE, plan, "op-1", resume=False)

    failed = evidence.model_copy(update={"status": StepStatus.FAILED})
    with pytest.raises(AdapterError, match="inconsistent outcome"):
        SubprocessPhaseExecutor(
            RecordingRunner([ChildResult((), 0, json.dumps(failed.as_artifact()))])
        ).execute(ProvisioningPhase.RESERVE, plan, "op-1", resume=False)


@pytest.mark.parametrize(
    ("return_code", "error"),
    [
        (1, HostOperationError),
        (2, PlanError),
        (3, SafetyError),
        (4, AdapterError),
        (5, RegistryConflictError),
        (6, UnknownProviderResult),
    ],
)
def test_subprocess_phase_executor_preserves_job_exit_meaning(
    tmp_path: Path,
    return_code: int,
    error: type[HostOperationError],
) -> None:
    plan = make_host_fixture(tmp_path).plan()
    with pytest.raises(error, match="phase job failed without evidence"):
        SubprocessPhaseExecutor(
            RecordingRunner([ChildResult((), return_code, "", "job diagnostic")])
        ).execute(ProvisioningPhase.RESERVE, plan, "op-1", resume=False)


@pytest.mark.parametrize(
    ("phase", "error"),
    [
        (ProvisioningPhase.ALLOCATE, UnknownProviderResult),
        (ProvisioningPhase.RESERVE, RegistryError),
        (ProvisioningPhase.CONFIGURATION_VERIFY, AdapterError),
    ],
)
def test_subprocess_phase_executor_maps_job_timeout_by_phase(
    tmp_path: Path,
    phase: ProvisioningPhase,
    error: type[HostOperationError],
) -> None:
    plan = make_host_fixture(tmp_path).plan()
    with pytest.raises(error, match="phase job failed without evidence"):
        SubprocessPhaseExecutor(
            RecordingRunner([ChildResult((), 124, "", "job timed out")])
        ).execute(phase, plan, "op-1", resume=False)


def test_registry_authority_renews_during_blocking_actions(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    operation = fixture.registry.create_operation(plan)
    authority = fixture.registry.acquire_locks(
        operation.id,
        ["resource/host-web01"],
    )
    fixture.registry.start_operation(operation.id, authority)
    renewed = Event()
    renew_locks = fixture.registry.renew_locks

    def renew():
        result = renew_locks(operation.id)
        renewed.set()
        return result

    fixture.registry.renew_locks = lambda _operation_id: renew()
    lease = _RegistryAuthorityLease(
        fixture.registry,
        operation.id,
        authority,
        renew_interval_seconds=0.001,
    )
    assert lease.run(lambda: renewed.wait(1))
    assert lease.authority.fencing_token > authority.fencing_token

    renewal_failed = Event()

    def fail_renewal(_operation_id):
        renewal_failed.set()
        raise RegistryError("lock renewal failed")

    fixture.registry.renew_locks = fail_renewal
    failed_lease = _RegistryAuthorityLease(
        fixture.registry,
        operation.id,
        lease.authority,
        renew_interval_seconds=0.001,
    )
    with pytest.raises(RegistryError, match="lock renewal failed"):
        failed_lease.run(lambda: renewal_failed.wait(1))


def test_phase_job_and_reconcile_job_mains(tmp_path: Path, monkeypatch, capsys) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    evidence = HostOperationEvidence(
        apiVersion="atlas.host-operation/v1",
        kind="HostOperationEvidence",
        operationId="op-1",
        planId=plan.metadata.plan_id,
        resourceId=plan.resource.id,
        phase="reserve",
        status="succeeded",
        startedAt=datetime.now(UTC),
        finishedAt=datetime.now(UTC),
        attempt=1,
    )
    monkeypatch.setattr("atlas_host_operations.controller.read_plan", lambda path: plan)
    monkeypatch.setattr(
        "atlas_host_operations.controller.execute_phase",
        lambda *args, **kwargs: PhaseExecution(evidence, 0),
    )
    monkeypatch.setattr(
        "atlas_host_operations.controller.load_registry_client",
        lambda _plan: fixture.registry,
    )
    monkeypatch.setattr(
        "atlas_host_operations.controller.provider_for_plan",
        lambda _plan: fixture.provider,
    )
    monkeypatch.setattr(
        "atlas_host_operations.controller.configurator_for_plan",
        lambda _plan: fixture.configurator,
    )
    assert (
        phase_job_main(
            ProvisioningPhase.RESERVE,
            ["--plan", "-", "--operation", "op-1", "--resume"],
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["phase"] == "reserve"
    monkeypatch.setattr(
        "atlas_host_operations.controller.read_plan",
        lambda path: (_ for _ in ()).throw(PlanError("bad plan")),
    )
    assert (
        phase_job_main(
            ProvisioningPhase.RESERVE,
            ["--plan", "-", "--operation", "op-1"],
        )
        == 2
    )
    assert "bad plan" in capsys.readouterr().err

    monkeypatch.setattr("atlas_host_operations.controller.read_plan", lambda path: plan)
    operation = fixture.registry.create_operation(plan)
    authority = fixture.registry.acquire_locks(
        operation.id,
        ["resource/host-web01"],
    )
    fixture.registry.start_operation(operation.id, authority)
    fixture.provider.exists = True
    fixture.provider.running = True
    fixture.provider.ownership_marker = True
    assert reconcile_job_main(["--plan", "-", "--operation", operation.id]) == 0
    assert json.loads(capsys.readouterr().out)["message"] == (
        "provider allocation recovered from live state"
    )


def test_factories_status_mapping_and_exception_exit_codes(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    with pytest.raises(AdapterError, match="unsupported provider"):
        provider_for_plan(
            plan.model_copy(
                update={
                    "provider": plan.provider.model_copy(update={"adapter": "unknown"})
                }
            )
        )
    with pytest.raises(AdapterError, match="unsupported configuration"):
        configurator_for_plan(
            plan.model_copy(
                update={
                    "configuration": plan.configuration.model_copy(
                        update={"adapter": "unknown"}
                    )
                }
            )
        )
    assert _host_lifecycle("ready") == "active"
    assert _host_lifecycle("absent") == "absent"
    assert _host_lifecycle("retired") == "retired"
    assert _host_lifecycle("configured") == "provisioning"

    operation = fixture.registry.create_operation(plan)
    assert _host_operation_status(operation) == "planned"
    blocked = operation.model_copy(
        update={
            "steps": [
                operation.steps[0].model_copy(update={"status": "blocked"}),
                *operation.steps[1:],
            ]
        }
    )
    assert _host_operation_status(blocked) == "needs-reconcile"
    succeeded = operation.model_copy(update={"status": "succeeded"})
    assert _host_operation_status(succeeded) == "completed"

    assert _exception_exit_code(SafetyError()) == 3
    assert _exception_exit_code(InputError()) == 2
    assert _exception_exit_code(PlanError()) == 2
    assert _exception_exit_code(RegistryConflictError()) == 5
    assert _exception_exit_code(UnknownProviderResult()) == 6
    assert _exception_exit_code(AdapterError()) == 4
    assert _exception_exit_code(RegistryAuthenticationError()) == 4
    assert _exception_exit_code(RegistryError()) == 5


def test_execute_phase_rejects_missing_operation_and_invalid_phase(
    tmp_path: Path,
) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    with pytest.raises(PlanError, match="not found"):
        execute_phase(
            ProvisioningPhase.RESERVE,
            plan,
            "missing",
            registry=fixture.registry,
            provider=fixture.provider,
            configurator=fixture.configurator,
            readiness=fixture.readiness,
            resume=False,
        )

    operation = fixture.registry.create_operation(plan)
    fixture.registry.acquire_locks(operation.id, ["resource/host-web01"])
    with pytest.raises(PlanError, match="not executable"):
        execute_phase(
            ProvisioningPhase.VALIDATE,
            plan,
            operation.id,
            registry=fixture.registry,
            provider=fixture.provider,
            configurator=fixture.configurator,
            readiness=fixture.readiness,
            resume=False,
        )
