from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from atlas_host_operations.artifacts import set_fingerprint
from atlas_host_operations.configurators import (
    AnsibleHostConfigurator,
    FakeHostConfigurator,
)
from atlas_host_operations.controller import _bootstrap_verification_context
from atlas_host_operations.errors import AdapterError, UnknownProviderResult
from atlas_host_operations.models import (
    HostOperationPlan,
    ProviderEvidence,
    ProviderObservation,
    RegistryAuthority,
)
from atlas_host_operations.providers import (
    FakeCloudProvider,
    HostContext,
    ProxmoxHostProvider,
)
from atlas_host_operations.readiness import (
    FakeReadinessChecker,
    HostReadinessChecker,
)
from atlas_host_operations.subprocesses import (
    ChildResult,
    RecordingRunner,
    SubprocessRunner,
)

from .test_host_operations_support import make_host_fixture


def _authority(plan: HostOperationPlan) -> RegistryAuthority:
    return RegistryAuthority(
        operationId="op-1",
        lockScope=f"resource/{plan.resource.id}",
        fencingToken=7,
        operationRevision=1,
        resourceRevision=1,
    )


def _proxmox_plan(plan: HostOperationPlan) -> HostOperationPlan:
    data = plan.as_artifact()
    data["provider"] = {
        "adapter": "proxmox",
        "resourceType": "proxmox.qemu",
        "plan": {
            "apiVersion": "atlas.operation/v1",
            "kind": "OperationPlan",
            "metadata": {
                "planId": "child-plan",
                "target": "web01",
                "site": "site01",
            },
            "provider": {"name": "proxmox", "node": "pve01"},
            "spec": {
                "vmid": 121,
                "name": "web01",
                "network": {"ip": "192.0.2.10"},
                "guest": {"sshPort": 22, "qemuAgent": True},
            },
        },
    }
    data["metadata"].pop("fingerprint", None)
    return set_fingerprint(HostOperationPlan.model_validate(data))


def _child_evidence() -> dict:
    return {
        "apiVersion": "atlas.operation/v1",
        "kind": "OperationEvidence",
        "createdResources": [
            {
                "id": "qemu/121",
                "name": "web01",
                "type": "proxmox.qemu",
                "node": "pve01",
                "vmid": 121,
                "ownershipMarkerWritten": True,
            }
        ],
        "rollback": {"supported": True},
    }


def _provider_evidence(plan: HostOperationPlan) -> ProviderEvidence:
    child = _child_evidence()
    return ProviderEvidence(
        provider="proxmox",
        resourceType="proxmox.qemu",
        resourceId="qemu/121",
        resourceName="web01",
        locator={"node": "pve01", "vmid": 121},
        ownershipMarker=True,
        details={"childEvidence": child, "childPlan": plan.provider.plan},
    )


def test_fake_provider_contract_and_failure_modes(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    context = HostContext(plan)
    provider = FakeCloudProvider()
    assert provider.planning_artifact() == {}
    assert provider.validate(context)[0].status == "passed"
    assert provider.planning_artifact()["target"] == "web01"
    evidence = provider.allocate(context, _authority(plan))
    assert evidence.provider == "fake-cloud"
    observation = provider.observe(context)
    assert observation.exists and observation.running
    assert observation.provider_evidence == evidence.model_copy(update={"details": {}})
    assert provider.verify(context, evidence).status == "passed"
    mismatched = evidence.model_copy(update={"resource_name": "other"})
    assert provider.verify(context, mismatched).status == "failed"
    missing_marker = evidence.model_copy(update={"ownership_marker": False})
    assert (
        provider.rollback(context, missing_marker, _authority(plan)).status == "failed"
    )
    assert provider.rollback(context, evidence, _authority(plan)).status == "succeeded"
    assert not provider.observe(context).exists

    for method in ("validate", "allocate", "observe", "verify", "rollback"):
        broken = FakeCloudProvider(fail_on=method)
        with pytest.raises(AdapterError, match=method):
            if method == "validate":
                broken.validate(context)
            elif method == "allocate":
                broken.allocate(context, _authority(plan))
            elif method == "observe":
                broken.observe(context)
            elif method == "verify":
                broken.verify(context, evidence)
            else:
                broken.rollback(context, evidence, _authority(plan))
    with pytest.raises(UnknownProviderResult, match="unknown"):
        FakeCloudProvider(unknown_allocate=True).allocate(context, _authority(plan))


def test_fake_configurator_contract_and_failures(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    context = HostContext(fixture.plan())
    configurator = FakeHostConfigurator()
    assert configurator.validate(context)[0].status == "passed"
    assert configurator.converge(context).status == "failed"
    assert configurator.verify(context).status == "failed"
    assert configurator.bootstrap(context).status == "succeeded"
    assert configurator.verify(context).status == "failed"
    assert configurator.converge(context).status == "succeeded"
    assert configurator.verify(context).status == "passed"
    for method in ("validate", "bootstrap", "converge", "verify"):
        broken = FakeHostConfigurator(fail_on=method)
        with pytest.raises(AdapterError, match=method):
            getattr(broken, method)(context)


def test_proxmox_adapter_exact_argv_and_successful_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLAS_EXECUTABLE", raising=False)
    monkeypatch.delenv("ATLAS_HOME", raising=False)
    fixture = make_host_fixture(tmp_path)
    plan = _proxmox_plan(fixture.plan())
    verify = {
        "status": "passed",
        "checks": [
            {"name": "proxmox.vm.exists", "status": "passed"},
            {"name": "proxmox.vm.running", "status": "passed"},
            {"name": "proxmox.ownership-marker", "status": "passed"},
            {"name": "proxmox.guest-agent", "status": "passed"},
            {"name": "proxmox.guest-agent.ip", "status": "passed"},
        ],
    }
    runner = RecordingRunner(
        [
            ChildResult((), 0, json.dumps(plan.provider.plan)),
            ChildResult((), 0, json.dumps(_child_evidence())),
            ChildResult((), 0, json.dumps(verify)),
            ChildResult((), 0, json.dumps(verify)),
            ChildResult(
                (),
                0,
                json.dumps({**_child_evidence(), "rollback": {"result": "success"}}),
            ),
        ]
    )
    provider = ProxmoxHostProvider(runner)
    context = HostContext(plan)
    assert provider.validate(context)[0].status == "passed"
    assert provider.planning_artifact()["kind"] == "OperationPlan"
    evidence = provider.allocate(context, _authority(plan))
    assert evidence.resource_id == "qemu/121"
    observation = provider.observe(context)
    assert observation.running
    assert observation.provider_evidence.resource_id == "qemu/121"
    assert provider.verify(context, evidence).status == "passed"
    assert provider.rollback(context, evidence, _authority(plan)).status == "succeeded"
    assert runner.calls[0]["argv"] == [
        "/opt/atlas/bin/atlas",
        "job",
        "run",
        "infrastructure-operations",
        "vm-create-plan",
        "--",
        plan.sources.provider_definition.path,
        plan.sources.provider_input.path,
    ]
    assert runner.calls[1]["argv"] == [
        "/opt/atlas/bin/atlas",
        "job",
        "run",
        "infrastructure-operations",
        "vm-create-apply",
        "--",
        plan.sources.provider_definition.path,
        "-",
        "--confirm",
        "child-plan",
    ]
    assert runner.calls[2]["argv"][-1] == "-"
    assert runner.calls[4]["argv"][4] == "vm-create-rollback"


@pytest.mark.parametrize(
    ("result", "status"),
    [
        (ChildResult((), 4, stderr="unavailable"), "failed"),
        (
            ChildResult(
                (),
                0,
                json.dumps({"metadata": {"planId": "x", "target": "other"}}),
            ),
            "failed",
        ),
    ],
)
def test_proxmox_validate_failure_results(
    tmp_path: Path,
    result: ChildResult,
    status: str,
) -> None:
    plan = _proxmox_plan(make_host_fixture(tmp_path).plan())
    provider = ProxmoxHostProvider(RecordingRunner([result]))
    assert provider.validate(HostContext(plan))[0].status == status


def test_proxmox_validate_rejects_invalid_json(tmp_path: Path) -> None:
    plan = _proxmox_plan(make_host_fixture(tmp_path).plan())
    provider = ProxmoxHostProvider(RecordingRunner([ChildResult((), 0, "not-json")]))
    with pytest.raises(AdapterError, match="invalid JSON"):
        provider.validate(HostContext(plan))


def test_proxmox_allocate_failure_boundaries(tmp_path: Path) -> None:
    plan = _proxmox_plan(make_host_fixture(tmp_path).plan())
    context = HostContext(plan)
    authority = _authority(plan)
    missing_id = plan.model_copy(
        update={"provider": plan.provider.model_copy(update={"plan": {}})}
    )
    with pytest.raises(AdapterError, match="plan ID"):
        ProxmoxHostProvider(RecordingRunner()).allocate(
            HostContext(missing_id), authority
        )
    with pytest.raises(UnknownProviderResult, match="timed out"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 124, timed_out=True)])
        ).allocate(context, authority)
    partial = {"createdResources": [{"id": "qemu/121"}]}
    with pytest.raises(UnknownProviderResult, match="provider evidence"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 1, json.dumps(partial))])
        ).allocate(context, authority)
    with pytest.raises(AdapterError, match="failed"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 4, stderr="no provider")])
        ).allocate(context, authority)
    with pytest.raises(UnknownProviderResult, match="valid evidence"):
        ProxmoxHostProvider(RecordingRunner([ChildResult((), 0, "not-json")])).allocate(
            context, authority
        )
    with pytest.raises(UnknownProviderResult, match="created resource"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 0, json.dumps({"createdResources": []}))])
        ).allocate(context, authority)
    invalid = _child_evidence()
    invalid["createdResources"][0]["name"] = ""
    with pytest.raises(UnknownProviderResult, match="invalid resource identity"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 0, json.dumps(invalid))])
        ).allocate(context, authority)
    missing_identity = plan.model_copy(
        update={
            "provider": plan.provider.model_copy(
                update={
                    "plan": {
                        **plan.provider.plan,
                        "spec": {},
                    }
                }
            )
        }
    )
    with pytest.raises(UnknownProviderResult, match="without a resource identity"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 0, json.dumps(_child_evidence()))])
        ).allocate(HostContext(missing_identity), authority)
    mismatched = _child_evidence()
    mismatched["createdResources"][0]["id"] = "qemu/999"
    with pytest.raises(UnknownProviderResult, match="does not match"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 0, json.dumps(mismatched))])
        ).allocate(context, authority)


def test_proxmox_observe_verify_and_rollback_failures(tmp_path: Path) -> None:
    plan = _proxmox_plan(make_host_fixture(tmp_path).plan())
    context = HostContext(plan)
    evidence = _provider_evidence(plan)
    failed_verify = {"status": "failed", "checks": []}
    provider = ProxmoxHostProvider(
        RecordingRunner([ChildResult((), 1, json.dumps(failed_verify))])
    )
    uncertain = provider.observe(context)
    assert not uncertain.exists
    assert not uncertain.absence_confirmed
    with pytest.raises(AdapterError, match="invalid checks"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 1, json.dumps({"status": "failed"}))])
        ).observe(context)
    live_without_identity = {
        "status": "failed",
        "checks": [
            {"name": "proxmox.vm.exists", "status": "passed"},
            {"name": "proxmox.ownership-marker", "status": "passed"},
        ],
    }
    invalid_plan = plan.model_copy(
        update={"provider": plan.provider.model_copy(update={"plan": {}})}
    )
    with pytest.raises(AdapterError, match="resource identity"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 1, json.dumps(live_without_identity))])
        ).observe(HostContext(invalid_plan))
    provider = ProxmoxHostProvider(
        RecordingRunner([ChildResult((), 1, json.dumps(failed_verify))])
    )
    assert provider.verify(context, evidence).status == "failed"
    missing = evidence.model_copy(update={"details": {}})
    with pytest.raises(AdapterError, match="child artifact"):
        ProxmoxHostProvider().verify(context, missing)
    with pytest.raises(AdapterError, match="timed out"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 124, timed_out=True)])
        ).observe(context)
    with pytest.raises(AdapterError, match="invalid JSON"):
        ProxmoxHostProvider(RecordingRunner([ChildResult((), 0, "invalid")])).observe(
            context
        )
    with pytest.raises(AdapterError, match="failed"):
        ProxmoxHostProvider(
            RecordingRunner(
                [ChildResult((), 4, json.dumps({"status": "failed"}), "bad")]
            )
        ).verify(context, evidence)
    with pytest.raises(AdapterError, match="invalid status"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 0, json.dumps({"status": "unknown"}))])
        ).verify(context, evidence)
    invalid_checks = {
        "status": "failed",
        "checks": [{"name": "", "status": "failed"}],
    }
    with pytest.raises(AdapterError, match="invalid checks"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 1, json.dumps(invalid_checks))])
        ).verify(context, evidence)

    no_marker = evidence.model_copy(update={"ownership_marker": False})
    assert (
        ProxmoxHostProvider().rollback(context, no_marker, _authority(plan)).status
        == "failed"
    )
    recovered = evidence.model_copy(
        update={"details": {"recoveredFromLiveState": True}}
    )
    retained = ProxmoxHostProvider().rollback(
        context,
        recovered,
        _authority(plan),
    )
    assert retained.status == "failed"
    assert "retained" in retained.message
    with pytest.raises(AdapterError, match="incomplete"):
        ProxmoxHostProvider().rollback(
            context,
            evidence.model_copy(update={"details": {}}),
            _authority(plan),
        )
    no_plan_id = evidence.model_copy(
        update={
            "details": {
                "childEvidence": _child_evidence(),
                "childPlan": {"metadata": {}},
            }
        }
    )
    with pytest.raises(AdapterError, match="plan ID"):
        ProxmoxHostProvider().rollback(context, no_plan_id, _authority(plan))
    with pytest.raises(UnknownProviderResult, match="timed out"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 124, timed_out=True)])
        ).rollback(context, evidence, _authority(plan))
    with pytest.raises(AdapterError, match="failed"):
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 4, "not-json", "bad")])
        ).rollback(context, evidence, _authority(plan))
    failed_rollback = {**_child_evidence(), "rollback": {"result": "failed"}}
    assert (
        ProxmoxHostProvider(
            RecordingRunner([ChildResult((), 1, json.dumps(failed_rollback))])
        )
        .rollback(context, evidence, _authority(plan))
        .status
        == "failed"
    )


def test_ansible_adapter_exact_argv_and_results(tmp_path: Path) -> None:
    fixture = make_host_fixture(tmp_path)
    plan = fixture.plan()
    context = HostContext(plan)
    runner = RecordingRunner(
        [
            ChildResult((), 0),
            ChildResult((), 2, stderr="bad playbook"),
            ChildResult((), 0),
            ChildResult((), 1, stderr="converge failed"),
            ChildResult((), 0),
            ChildResult((), 0),
        ]
    )
    adapter = AnsibleHostConfigurator(runner)
    checks = adapter.validate(context)
    assert [check.status for check in checks] == ["passed", "failed"]
    assert adapter.bootstrap(context).status == "succeeded"
    assert adapter.converge(context).status == "failed"
    assert adapter.verify(context).status == "passed"
    assert adapter.verify(_bootstrap_verification_context(context)).status == "passed"
    assert runner.calls[0]["argv"] == ["configctl", "validate", "bootstrap"]
    assert runner.calls[2]["argv"] == [
        "configctl",
        "apply",
        "bootstrap",
        "web01",
    ]
    assert runner.calls[2]["timeout_seconds"] == 1800
    assert runner.calls[3]["timeout_seconds"] == 3600
    assert runner.calls[4]["argv"] == ["configctl", "check", "site", "web01"]
    assert runner.calls[5]["argv"] == [
        "configctl",
        "check",
        "bootstrap",
        "web01",
    ]
    assert all(call["cwd"] == fixture.project for call in runner.calls)
    timed_out = AnsibleHostConfigurator(
        RecordingRunner([ChildResult((), 124, timed_out=True)])
    )
    with pytest.raises(AdapterError, match="timed out"):
        timed_out.bootstrap(context)
    failed_verify = AnsibleHostConfigurator(
        RecordingRunner([ChildResult((), 1, stderr="check failed")])
    )
    assert failed_verify.verify(context).status == "failed"


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_readiness_checks_provider_tcp_ssh_and_cloud_init(tmp_path: Path) -> None:
    plan = make_host_fixture(tmp_path).plan()
    context = HostContext(plan)
    observation = ProviderObservation(
        exists=True,
        running=True,
        guestAgentReady=True,
        addresses=[plan.readiness.address],
    )
    attempts = iter([OSError("not ready"), _Connection()])

    def connector(target, timeout):
        result = next(attempts)
        if isinstance(result, BaseException):
            raise result
        return result

    times = iter([0.0, 0.1, 0.2])
    runner = RecordingRunner([ChildResult((), 0), ChildResult((), 0)])
    checker = HostReadinessChecker(
        runner,
        connector=connector,
        monotonic=lambda: next(times),
        sleeper=lambda _seconds: None,
        timeout_seconds=10,
    )
    result = checker.wait(context, observation)
    assert result.status == "passed"
    assert runner.calls[0]["argv"][-1] == "true"
    assert runner.calls[1]["argv"][-3:] == ["cloud-init", "status", "--wait"]

    failed_provider = observation.model_copy(update={"exists": False, "running": False})
    assert HostReadinessChecker().wait(context, failed_provider).status == "failed"
    no_agent = observation.model_copy(update={"guest_agent_ready": False})
    assert HostReadinessChecker().wait(context, no_agent).status == "failed"
    wrong_address = observation.model_copy(update={"addresses": ["192.0.2.11"]})
    assert HostReadinessChecker().wait(context, wrong_address).status == "failed"
    tcp_timeout = HostReadinessChecker(
        connector=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
        monotonic=lambda: 10.0,
        sleeper=lambda _seconds: None,
        timeout_seconds=0,
    )
    assert tcp_timeout.wait(context, observation).status == "failed"

    ssh_failed = HostReadinessChecker(
        RecordingRunner([ChildResult((), 1)]),
        connector=lambda *_args, **_kwargs: _Connection(),
    )
    assert ssh_failed.wait(context, observation).status == "failed"
    cloud_failed = HostReadinessChecker(
        RecordingRunner([ChildResult((), 0), ChildResult((), 1)]),
        connector=lambda *_args, **_kwargs: _Connection(),
    )
    assert cloud_failed.wait(context, observation).status == "failed"
    fake = FakeReadinessChecker(status="failed")
    assert fake.wait(context, observation).status == "failed"


def test_subprocess_and_recording_runners(tmp_path: Path, capsys) -> None:
    runner = SubprocessRunner()
    result = runner.run(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ],
        cwd=tmp_path,
        env={"HOSTCTL_TEST": "1"},
        input_text="",
        timeout_seconds=5,
    )
    assert result.return_code == 0
    assert result.stdout.strip() == "out"
    assert "err" in capsys.readouterr().err
    missing = runner.run([str(tmp_path / "missing")])
    assert missing.return_code == 127
    timeout = runner.run(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        timeout_seconds=0.01,
    )
    assert timeout.timed_out and timeout.return_code == 124
    recording = RecordingRunner()
    with pytest.raises(AssertionError, match="no recorded result"):
        recording.run(["anything"])
