from __future__ import annotations

import builtins
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest
from atlas_operations.operation.config import ProxmoxProviderConfig
from atlas_operations.operation.errors import ProviderError
from atlas_operations.operation.plan import CheckResult, OperationPlan, OperationStep
from atlas_operations.operation.provider import ProviderQuery
from atlas_operations.operation.proxmox.client import (
    ProxmoxApiTransport,
    ProxmoxProviderClient,
    _agent_enabled,
    _bridge_allows_vlan,
    _detect_imported_disk,
    _disk_spec,
    _has_cloud_init_drive,
    _has_permission,
    _node_for,
    _permission_checks,
    _permission_map,
    _permission_path_applies,
    _required_step_param,
    _single_unused_disk,
    _tcp_check,
    _template_net0,
    _unused_disks,
    _verify_rollback_live_config,
    _vm_names,
    _vmids,
)
from atlas_operations.operation.proxmox.cloudinit import ipconfig0, nameserver
from atlas_operations.operation.proxmox.ownership import (
    marker_matches,
    ownership_marker,
    vm_tags,
)
from atlas_operations.operation.proxmox.tasks import wait_for_task
from atlas_operations.operation.vm_create import build_vm_create_plan
from atlas_operations.operation.vm_template_create import build_vm_template_create_plan

from .test_operation_support import FakeProvider, FakeTransport, write_operation_inputs


def _provider_config() -> ProxmoxProviderConfig:
    return ProxmoxProviderConfig(
        api_url="https://pve.example.invalid:8006/api2/json",
        verify_ssl=True,
        token_id_ref="env:PROXMOX_TOKEN_ID",
        token_secret_ref="env:PROXMOX_TOKEN_SECRET",
        task_timeout_seconds=1,
        poll_interval_seconds=1,
    )


def _plans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[OperationPlan, OperationPlan]:
    (
        provider_path,
        vm_input_path,
        template_input_path,
        definition,
        vm_input,
        template_input,
    ) = write_operation_inputs(tmp_path)
    monkeypatch.setattr(
        "atlas_operations.operation.vm_create._ping_check",
        lambda ip: CheckResult(name="network.ip.unused", status="passed", message=ip),
    )
    return (
        build_vm_create_plan(
            definition,
            vm_input,
            input_path=str(vm_input_path),
            provider_path=str(provider_path),
            provider=FakeProvider(),
        ),
        build_vm_template_create_plan(
            definition,
            template_input,
            input_path=str(template_input_path),
            provider_path=str(provider_path),
            provider=FakeProvider(),
        ),
    )


class Endpoint:
    def __init__(self, path: tuple[Any, ...] = ()) -> None:
        self.path = path

    def __getattr__(self, name: str) -> Endpoint:
        return Endpoint((*self.path, name))

    def __call__(self, *args: Any) -> Endpoint:
        return Endpoint((*self.path, *args))

    def get(self, **kwargs: Any) -> Any:
        if self.path == ("nodes",):
            return [{"node": "pve01"}]
        if self.path == ("cluster", "resources"):
            return [{"vmid": 121}]
        if self.path[-1:] == ("config",):
            return {"name": "web01"}
        if self.path[-2:] == ("status", "current"):
            return {"status": "running"}
        if self.path[-1:] == ("storage",):
            return [{"storage": "local-lvm"}]
        if self.path == ("pools",):
            return [{"poolid": "pool"}]
        if self.path[-1:] == ("network",):
            return [{"iface": "vmbr0"}]
        if self.path == ("access", "permissions"):
            return {"/": {"VM.Audit": 1}}
        if "tasks" in self.path:
            return {"status": "stopped", "exitstatus": "OK"}
        if "agent" in self.path:
            return {"result": []}
        raise AssertionError((self.path, kwargs))

    def post(self, **kwargs: Any) -> str:
        return "UPID:post"

    def put(self, **kwargs: Any) -> str:
        return "UPID:put"

    def delete(self, **kwargs: Any) -> str:
        return "UPID:delete"


def test_api_transport_connects_with_secret_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def api(*args: Any, **kwargs: Any) -> Endpoint:
        calls.append((args, kwargs))
        return Endpoint()

    monkeypatch.setitem(sys.modules, "proxmoxer", SimpleNamespace(ProxmoxAPI=api))
    monkeypatch.setenv("PROXMOX_TOKEN_ID", "ops@pve!atlas")
    monkeypatch.setenv("PROXMOX_TOKEN_SECRET", "secret")
    transport = ProxmoxApiTransport(_provider_config())

    assert calls[0] == (
        ("pve.example.invalid",),
        {
            "user": "ops@pve",
            "token_name": "atlas",
            "token_value": "secret",
            "verify_ssl": True,
            "port": 8006,
            "service": "PVE",
        },
    )
    assert transport.nodes() == [{"node": "pve01"}]
    assert transport.vms() == [{"vmid": 121}]
    assert transport.vm_config("pve01", 121) == {"name": "web01"}
    assert transport.vm_status("pve01", 121) == {"status": "running"}
    assert transport.storages("pve01") == [{"storage": "local-lvm"}]
    assert transport.pools() == [{"poolid": "pool"}]
    assert transport.bridges("pve01") == [{"iface": "vmbr0"}]
    assert transport.permissions() == {"/": {"VM.Audit": 1}}
    assert transport.clone_vm("pve01", 9000, {"newid": 121}) == "UPID:post"
    assert transport.set_vm_config("pve01", 121, {"cores": 2}) == "UPID:put"
    assert transport.create_vm("pve01", {"vmid": 121}) == "UPID:post"
    assert (
        transport.import_disk("pve01", 121, "/srv/image", "local-lvm")
        == "UPID:post"
    )
    assert transport.resize_disk("pve01", 121, "scsi0", "20G") == "UPID:put"
    assert transport.start_vm("pve01", 121) == "UPID:post"
    assert transport.stop_vm("pve01", 121) == "UPID:post"
    assert transport.delete_vm("pve01", 121) == "UPID:delete"
    assert transport.convert_template("pve01", 121) == "UPID:post"
    assert transport.task_status("pve01", "UPID")["status"] == "stopped"
    assert transport.guest_agent_network("pve01", 121) == {"result": []}


def test_api_transport_reports_dependency_and_configuration_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "proxmoxer":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(ProviderError, match="proxmoxer"):
        ProxmoxApiTransport(_provider_config())
    monkeypatch.setattr(builtins, "__import__", original_import)

    monkeypatch.setitem(
        sys.modules,
        "proxmoxer",
        SimpleNamespace(ProxmoxAPI=lambda *args, **kwargs: Endpoint()),
    )
    incomplete = ProxmoxProviderConfig.model_construct(
        api_url="",
        token_id_ref="",
        token_secret_ref="",
        verify_ssl=True,
        task_timeout_seconds=1,
        poll_interval_seconds=1,
    )
    with pytest.raises(ProviderError, match="missing API"):
        ProxmoxApiTransport(incomplete)

    monkeypatch.setenv("PROXMOX_TOKEN_ID", "invalid")
    monkeypatch.setenv("PROXMOX_TOKEN_SECRET", "secret")
    with pytest.raises(ProviderError, match="token id"):
        ProxmoxApiTransport(_provider_config())

    def failed_api(*args: Any, **kwargs: Any) -> Endpoint:
        raise RuntimeError("client initialization failed")

    monkeypatch.setitem(
        sys.modules,
        "proxmoxer",
        SimpleNamespace(ProxmoxAPI=failed_api),
    )
    monkeypatch.setenv("PROXMOX_TOKEN_ID", "ops@pve!atlas")
    with pytest.raises(ProviderError, match="initialize Proxmox API"):
        ProxmoxApiTransport(_provider_config())


def test_provider_status_capabilities_and_query_errors() -> None:
    client = ProxmoxProviderClient(_provider_config(), transport=FakeTransport())
    assert client.capabilities().live_operations == [
        "proxmox.vm-create",
        "proxmox.vm-template-create",
    ]
    state = client.read_state(ProviderQuery(kind="status"))
    assert state.data["node_count"] == 1
    assert state.data["vm_count"] == 1
    with pytest.raises(ProviderError, match="unsupported Proxmox query"):
        client.read_state(ProviderQuery(kind="unknown"))

    class FailedStatusTransport:
        def nodes(self) -> list[dict[str, Any]]:
            raise RuntimeError("status failed")

    failed = ProxmoxProviderClient(
        _provider_config(),
        transport=FailedStatusTransport(),
    )
    with pytest.raises(ProviderError, match="read Proxmox status"):
        failed.read_state(ProviderQuery(kind="status"))


def test_provider_preflight_checks_vm_and_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, template_plan = _plans(tmp_path, monkeypatch)
    client = ProxmoxProviderClient(_provider_config(), transport=FakeTransport())

    vm_result = client.preflight(vm_plan)
    assert vm_result.status == "passed"
    assert {check.name for check in vm_result.checks} >= {
        "proxmox.template.is-template",
        "proxmox.template.cloud-init-drive",
        "proxmox.permissions.write",
    }
    template_result = client.preflight(template_plan)
    assert template_result.status == "passed"
    assert {check.name for check in template_result.checks} >= {
        "template.image.sha256",
        "template.guest.qemu-agent",
        "template.guest.serial-console",
    }


def test_provider_preflight_collects_live_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, template_plan = _plans(tmp_path, monkeypatch)
    transport = FakeTransport()
    transport.nodes = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    client = ProxmoxProviderClient(_provider_config(), transport=transport)
    assert client.preflight(vm_plan).checks[-1].name == "proxmox.api"
    assert client.preflight(template_plan).checks[-1].name == "proxmox.api"

    transport = FakeTransport()
    transport.cluster_vms = []
    transport.storages = lambda node: []
    transport.bridges = lambda node: []
    transport.permissions = lambda: {}
    client = ProxmoxProviderClient(_provider_config(), transport=transport)
    result = client.preflight(vm_plan)
    assert result.status == "failed"
    assert next(
        check for check in result.checks if check.name == "proxmox.template.exists"
    ).status == "failed"

    result = client.preflight(template_plan)
    assert result.status == "failed"
    assert next(
        check for check in result.checks if check.name == "proxmox.storage.exists"
    ).status == "failed"


def test_provider_preflight_handles_template_config_failure_and_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, template_plan = _plans(tmp_path, monkeypatch)
    transport = FakeTransport()
    transport.vm_config = lambda node, vmid: (_ for _ in ()).throw(RuntimeError("denied"))
    transport.storages = lambda node: [{"storage": "local-lvm", "avail": 1}]
    transport.bridges = lambda node: [
        {"iface": "vmbr0", "bridge_vlan_aware": "no"}
    ]
    transport.permissions = lambda: {"/": {}}
    client = ProxmoxProviderClient(_provider_config(), transport=transport)

    result = client.preflight(vm_plan)
    assert result.status == "failed"
    assert next(
        check for check in result.checks if check.name == "proxmox.template.config"
    ).status == "failed"
    assert next(
        check for check in result.checks if check.name == "proxmox.storage.free"
    ).status == "failed"
    assert next(
        check for check in result.checks if check.name == "proxmox.pool.exists"
    ).status == "passed"

    result = client.preflight(template_plan)
    assert result.status == "failed"
    assert next(
        check for check in result.checks if check.name == "proxmox.bridge.vlan-aware"
    ).status == "failed"


def test_provider_preflight_optional_vm_fields_are_really_optional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, _template_plan = _plans(tmp_path, monkeypatch)
    plan = _copy_plan(
        vm_plan,
        lambda data: (
            data["spec"].pop("templateName"),
            data["spec"].update(pool=None),
            data["spec"]["guest"].update(qemuAgent=False),
        ),
    )
    client = ProxmoxProviderClient(_provider_config(), transport=FakeTransport())
    assert client.preflight(plan).status == "passed"


def test_provider_executes_every_apply_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, template_plan = _plans(tmp_path, monkeypatch)
    transport = FakeTransport()
    client = ProxmoxProviderClient(_provider_config(), transport=transport)
    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.client.socket.create_connection",
        lambda *args, **kwargs: _Socket(),
    )

    for step in vm_plan.apply.steps:
        assert client.apply_step(step, vm_plan).status == "success"

    for step in template_plan.apply.steps:
        if step.action in {"download-image", "verify-image-checksum"}:
            continue
        if step.action == "template-import-disk":
            step = step.model_copy(update={"params": {"imagePath": "/srv/image.img"}})
        result = client.apply_step(step, template_plan)
        assert result.status == "success", (step.action, result.message)

    unsupported = OperationStep(
        id="unsupported",
        description="Unsupported",
        provider="proxmox",
        action="unsupported",
    )
    result = client.apply_step(unsupported, vm_plan)
    assert result.status == "failed"
    assert "unsupported" in result.message


def test_provider_apply_optional_and_fallback_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, template_plan = _plans(tmp_path, monkeypatch)
    transport = FakeTransport()
    transport.set_vm_config = lambda *args, **kwargs: None
    transport.resize_disk = lambda *args, **kwargs: None
    client = ProxmoxProviderClient(_provider_config(), transport=transport)

    no_optional = _copy_plan(
        vm_plan,
        lambda data: (
            data["spec"].update(pool=None),
            data["spec"]["disk"].update(storage=""),
            data["spec"]["cloudInit"].update(
                sshPublicKeys=[],
                ciupgrade=True,
            ),
            data["spec"]["network"].update(dnsServers=[]),
        ),
    )
    for action in ("clone-template", "set-cloud-init", "set-agent"):
        step = next(step for step in no_optional.apply.steps if step.action == action)
        assert client.apply_step(step, no_optional).status == "success"

    transport.config = {"unused0": "local-lvm:vm-9100-disk-0"}
    attach = next(
        step
        for step in template_plan.apply.steps
        if step.action == "template-attach-disk"
    )
    assert client.apply_step(attach, template_plan).status == "success"


def test_provider_verify_vm_and_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, template_plan = _plans(tmp_path, monkeypatch)
    transport = FakeTransport()
    transport.config = {
        "name": "web01",
        "status": "running",
        "tags": ";".join(vm_plan.spec["tags"]),
        "description": ownership_marker(
            vm_plan.metadata.plan_id,
            vm_plan.metadata.operation_kind,
            vm_plan.metadata.target,
        ),
    }
    client = ProxmoxProviderClient(_provider_config(), transport=transport)
    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.client.socket.create_connection",
        lambda *args, **kwargs: _Socket(),
    )
    assert client.verify(vm_plan).status == "passed"

    transport.config = {
        "name": template_plan.spec["name"],
        "template": 1,
        "scsi0": "local-lvm:vm-9100-disk-0",
        "ide2": "local-lvm:cloudinit,media=cdrom",
        "agent": "enabled=1",
        "tags": ";".join(template_plan.spec["tags"]),
        "description": ownership_marker(
            template_plan.metadata.plan_id,
            template_plan.metadata.operation_kind,
            template_plan.metadata.target,
        ),
    }
    assert client.verify(template_plan).status == "passed"


def test_provider_verify_reports_missing_and_guest_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, template_plan = _plans(tmp_path, monkeypatch)
    transport = FakeTransport()
    transport.vm_config = lambda *args: (_ for _ in ()).throw(KeyError("missing"))
    client = ProxmoxProviderClient(_provider_config(), transport=transport)
    assert client.verify(vm_plan).status == "failed"
    assert client.verify(template_plan).status == "failed"

    transport = FakeTransport()
    transport.config = {"name": "wrong"}
    transport.guest_agent_network = lambda *args: (_ for _ in ()).throw(
        RuntimeError("agent down")
    )
    client = ProxmoxProviderClient(_provider_config(), transport=transport)
    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.client.socket.create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("closed")),
    )
    result = client.verify(vm_plan)
    assert result.status == "failed"
    assert next(
        check for check in result.checks if check.name == "proxmox.guest-agent"
    ).status == "failed"
    assert next(check for check in result.checks if check.name == "ssh.tcp").status == "failed"

    without_agent = _copy_plan(
        vm_plan,
        lambda data: data["spec"]["guest"].update(qemuAgent=False),
    )
    transport = FakeTransport()
    transport.config = {
        "name": "web01",
        "status": "running",
        "tags": ";".join(without_agent.spec["tags"]),
        "description": ownership_marker(
            without_agent.metadata.plan_id,
            without_agent.metadata.operation_kind,
            without_agent.metadata.target,
        ),
    }
    client = ProxmoxProviderClient(_provider_config(), transport=transport)
    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.client.socket.create_connection",
        lambda *args, **kwargs: _Socket(),
    )
    assert client.verify(without_agent).status == "passed"


def test_provider_rollback_actions_require_matching_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, template_plan = _plans(tmp_path, monkeypatch)
    transport = FakeTransport()
    transport.config = {
        "vmid": 121,
        "name": "web01",
        "description": ownership_marker(
            vm_plan.metadata.plan_id,
            vm_plan.metadata.operation_kind,
            vm_plan.metadata.target,
        ),
    }
    client = ProxmoxProviderClient(_provider_config(), transport=transport)
    verify = _rollback_verification(vm_plan, "verify-created-resource", "proxmox.qemu")
    assert client.rollback_step(verify, vm_plan).status == "success"

    stop = next(step for step in vm_plan.rollback.steps if step.action == "stop-vm")
    assert client.rollback_step(stop, vm_plan).status == "success"
    delete = next(step for step in vm_plan.rollback.steps if step.action == "delete-vm")
    assert client.rollback_step(delete, vm_plan).status == "success"
    verify_deleted = next(
        step for step in vm_plan.rollback.steps if step.action == "verify-deleted"
    )
    assert client.rollback_step(verify_deleted, vm_plan).status == "success"

    template_transport = FakeTransport()
    template_transport.config = {
        "vmid": 9100,
        "name": template_plan.spec["name"],
    }
    template_client = ProxmoxProviderClient(
        _provider_config(),
        transport=template_transport,
    )
    verify_template = _rollback_verification(
        template_plan,
        "verify-created-template",
        "proxmox.qemu-template",
        created_by="create-vm",
        marker_written=False,
    )
    assert (
        template_client.rollback_step(verify_template, template_plan).status
        == "success"
    )

    unsupported = OperationStep(
        id="bad",
        description="Bad",
        provider="proxmox",
        action="bad",
    )
    assert client.rollback_step(unsupported, vm_plan).status == "failed"


def test_provider_rollback_rejects_wrong_marker_and_existing_deleted_vm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, _template_plan = _plans(tmp_path, monkeypatch)
    transport = FakeTransport()
    transport.config = {
        "vmid": 121,
        "name": "web01",
        "description": ownership_marker(
            "other-plan",
            vm_plan.metadata.operation_kind,
            vm_plan.metadata.target,
        ),
    }
    client = ProxmoxProviderClient(_provider_config(), transport=transport)
    verify = _rollback_verification(vm_plan, "verify-created-resource", "proxmox.qemu")
    result = client.rollback_step(verify, vm_plan)
    assert result.status == "failed"
    assert "ownership marker" in result.message

    unmarked_evidence = verify.model_copy(
        update={
            "params": {
                **verify.params,
                "evidenceOwnershipMarkerWritten": False,
            }
        }
    )
    result = client.rollback_step(unmarked_evidence, vm_plan)
    assert result.status == "failed"
    assert "ownership marker" in result.message

    verify_deleted = next(
        step for step in vm_plan.rollback.steps if step.action == "verify-deleted"
    )
    assert client.rollback_step(verify_deleted, vm_plan).status == "failed"

    transport.config = {"vmid": 121, "name": "web01", "status": "stopped"}
    stop = next(step for step in vm_plan.rollback.steps if step.action == "stop-vm")
    assert client.rollback_step(stop, vm_plan).status == "success"


def test_provider_wait_helpers_timeout_and_recover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, _template_plan = _plans(tmp_path, monkeypatch)
    transport = FakeTransport()
    client = ProxmoxProviderClient(_provider_config(), transport=transport)
    attempts = iter([RuntimeError("booting"), {"result": []}])

    def guest(*args):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    transport.guest_agent_network = guest
    times = iter([0.0, 0.1, 0.2])
    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.client.time.monotonic",
        lambda: next(times),
    )
    monkeypatch.setattr("atlas_operations.operation.proxmox.client.time.sleep", lambda _: None)
    client._wait_guest_agent("pve01", 121)

    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.client.socket.create_connection",
        lambda *args, **kwargs: _Socket(),
    )
    times = iter([0.0, 0.1])
    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.client.time.monotonic",
        lambda: next(times),
    )
    client._wait_ssh("192.0.2.21", 22)

    failing = FakeTransport()
    failing.guest_agent_network = lambda *args: (_ for _ in ()).throw(
        RuntimeError("down")
    )
    client = ProxmoxProviderClient(_provider_config(), transport=failing)
    times = iter([0.0, 2.0])
    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.client.time.monotonic",
        lambda: next(times),
    )
    with pytest.raises(ProviderError, match="did not respond"):
        client._wait_guest_agent("pve01", 121)

    times = iter([0.0, 0.1, 2.0])
    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.client.time.monotonic",
        lambda: next(times),
    )
    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.client.socket.create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("closed")),
    )
    with pytest.raises(ProviderError, match="not reachable"):
        client._wait_ssh("192.0.2.21", 22)

    assert client.apply_step(
        next(step for step in vm_plan.apply.steps if step.action == "wait-ssh"),
        vm_plan,
    ).status == "failed"


def test_proxmox_helpers_cover_strict_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, _template_plan = _plans(tmp_path, monkeypatch)
    assert _vmids([{"vmid": "1"}, {"vmid": None}]) == {1}
    assert _vm_names([{"name": "web"}, {}]) == {"web"}
    assert _disk_spec(vm_plan.spec)["device"] == "scsi0"
    with pytest.raises(ProviderError, match="does not define disk"):
        _disk_spec({})
    with pytest.raises(ProviderError, match="missing"):
        _disk_spec({"disk": {"device": "scsi0"}})

    step = OperationStep(
        id="step",
        description="Step",
        provider="proxmox",
        action="action",
    )
    with pytest.raises(ProviderError, match="requires param"):
        _required_step_param(step, "value")
    assert _node_for(vm_plan) == "pve01"
    no_node = vm_plan.model_copy(
        update={
            "provider": vm_plan.provider.model_copy(update={"node": ""}),
            "spec": {**vm_plan.spec, "node": ""},
        }
    )
    with pytest.raises(ProviderError, match="does not define"):
        _node_for(no_node)

    assert _detect_imported_disk({}, {"unused0": "local:disk"}) == "local:disk"
    with pytest.raises(ProviderError, match="could not be determined"):
        _detect_imported_disk({}, {})
    assert _single_unused_disk({"unused0": "local:disk,size=1G"}) == "local:disk"
    with pytest.raises(ProviderError, match="exactly one"):
        _single_unused_disk({})
    assert _unused_disks({"unused0": "local:disk,size=1G", "name": "vm"}) == {
        "unused0": "local:disk"
    }
    assert _template_net0(
        {"network": {"bridge": "vmbr0", "vlan": 130}}
    ) == "virtio,bridge=vmbr0,tag=130"
    assert _has_cloud_init_drive({"ide2": "local:cloudinit"})
    assert not _has_cloud_init_drive({})
    for value in (1, True, "1", "enabled=1"):
        assert _agent_enabled(value)
    assert not _agent_enabled("disabled")
    for value in (1, True, "yes", "on"):
        assert _bridge_allows_vlan({"bridge_vlan_aware": value})
    assert not _bridge_allows_vlan({"bridge_vlan_aware": "no"})

    grants = _permission_map(
        {
            "nested": [
                {"/nodes": {"Sys.Audit": 1, "ignored": 1}},
                {"other": "value"},
            ]
        }
    )
    assert grants == {"/nodes": {"Sys.Audit"}}
    assert _has_permission(grants, "Sys.Audit", "/nodes/pve01")
    assert not _has_permission(grants, "VM.Audit", "/nodes/pve01")
    assert _permission_path_applies("/", "/vms/1")
    assert _permission_path_applies("/vms", "/vms/1")
    assert not _permission_path_applies("/nodes", "/vms/1")
    checks = _permission_checks({}, node="pve01", storage="local", vmid=121)
    assert all(check.status == "failed" for check in checks)

    assert ipconfig0(vm_plan.spec) == "ip=192.0.2.21/24,gw=192.0.2.1"
    assert nameserver(vm_plan.spec) == "192.0.2.53"
    no_dns = {**vm_plan.spec, "network": {**vm_plan.spec["network"], "dnsServers": []}}
    assert nameserver(no_dns) is None

    marker = ownership_marker("plan", "kind", "target")
    assert marker_matches(marker, "plan", "kind", "target")
    assert not marker_matches(None, "plan", "kind", "target")
    assert vm_tags({"tags": "a;b,c"}) == {"a", "b", "c"}
    assert vm_tags({"tags": ["a", 2]}) == {"a", "2"}

    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.client.socket.create_connection",
        lambda *args, **kwargs: _Socket(),
    )
    assert _tcp_check("127.0.0.1", 22).status == "passed"
    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.client.socket.create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("closed")),
    )
    assert _tcp_check("127.0.0.1", 22).status == "failed"


def test_task_waiter_success_failure_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    assert wait_for_task(transport, "pve01", "UPID", 1, 1)["exitstatus"] == "OK"
    transport.task_result = {"status": "stopped", "exitstatus": "ERROR"}
    with pytest.raises(ProviderError, match="task failed"):
        wait_for_task(transport, "pve01", "UPID", 1, 1)

    transport.task_result = {"status": "running"}
    times = iter([0.0, 0.1, 2.0])
    monkeypatch.setattr(
        "atlas_operations.operation.proxmox.tasks.time.monotonic",
        lambda: next(times),
    )
    monkeypatch.setattr("atlas_operations.operation.proxmox.tasks.time.sleep", lambda _: None)
    with pytest.raises(ProviderError, match="timed out"):
        wait_for_task(transport, "pve01", "UPID", 1, 1)


def test_rollback_live_config_rejects_each_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm_plan, _template_plan = _plans(tmp_path, monkeypatch)
    base = _rollback_verification(vm_plan, "verify-created-resource", "proxmox.qemu")
    config = {"vmid": 121, "name": "web01"}
    _verify_rollback_live_config(base, vm_plan, config)

    updates = [
        ("evidenceResourceId", "qemu/999", "resource id"),
        ("evidenceResourceType", "wrong", "resource type"),
        ("evidenceResourceVmid", 999, "VMID"),
        ("evidenceResourceName", "wrong", "resource name"),
        ("evidenceResourceNode", "wrong", "resource node"),
        ("evidenceCreatedByStep", "wrong", "rollback-safe"),
    ]
    for key, value, message in updates:
        bad = base.model_copy(update={"params": {**base.params, key: value}})
        with pytest.raises(ProviderError, match=message):
            _verify_rollback_live_config(bad, vm_plan, config)

    with pytest.raises(ProviderError, match=r"Live VMID|live VMID"):
        _verify_rollback_live_config(base, vm_plan, {"vmid": 999, "name": "web01"})
    with pytest.raises(ProviderError, match="VM name"):
        _verify_rollback_live_config(base, vm_plan, {"vmid": 121, "name": "wrong"})


def _copy_plan(plan: OperationPlan, update) -> OperationPlan:
    data = plan.as_artifact()
    update(data)
    return OperationPlan.model_validate(data)


def _rollback_verification(
    plan: OperationPlan,
    action: str,
    resource_type: str,
    *,
    created_by: str = "clone-template",
    marker_written: bool = True,
) -> OperationStep:
    return OperationStep(
        id=action,
        description="Verify created resource",
        provider="proxmox",
        action=action,
        params={
            "evidenceResourceId": f"qemu/{plan.spec['vmid']}",
            "evidenceResourceType": resource_type,
            "evidenceResourceName": plan.spec["name"],
            "evidenceResourceNode": plan.provider.node,
            "evidenceResourceVmid": plan.spec["vmid"],
            "evidenceCreatedByStep": created_by,
            "evidenceOwnershipMarkerWritten": marker_written,
        },
    )


class _Socket:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False
