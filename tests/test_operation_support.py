from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from atlas_operations.operation.config import (
    ProviderDefinition,
    VmCreateInput,
    VmTemplateCreateInput,
    load_provider_definition,
    load_vm_create_input,
    load_vm_template_create_input,
)
from atlas_operations.operation.plan import CheckResult, OperationPlan, OperationStep
from atlas_operations.operation.provider import StepResult, VerifyResult


class FakeProvider:
    name = "proxmox"

    def __init__(
        self,
        *,
        fail_on: str | None = None,
        rollback_fail_on: str | None = None,
        preflight_status: str = "passed",
        verify_status: str = "passed",
    ) -> None:
        self.fail_on = fail_on
        self.rollback_fail_on = rollback_fail_on
        self.preflight_status = preflight_status
        self.verify_status = verify_status
        self.applied: list[str] = []
        self.rolled_back: list[str] = []

    def preflight(self, plan: OperationPlan) -> VerifyResult:
        return VerifyResult(
            status=self.preflight_status,
            checks=[
                CheckResult(
                    name="provider.preflight",
                    status=self.preflight_status,
                    message=plan.metadata.operation_kind,
                )
            ],
        )

    def apply_step(self, step: OperationStep, plan: OperationPlan) -> StepResult:
        self.applied.append(step.id)
        if step.id == self.fail_on:
            return StepResult(id=step.id, status="failed", message=f"failed at {step.id}")
        return StepResult(id=step.id, status="success", task_id=f"task-{step.id}")

    def verify(self, plan: OperationPlan) -> VerifyResult:
        return VerifyResult(
            status=self.verify_status,
            checks=[
                CheckResult(
                    name="provider.verify",
                    status=self.verify_status,
                    message=plan.metadata.operation_kind,
                )
            ],
        )

    def rollback_step(self, step: OperationStep, plan: OperationPlan) -> StepResult:
        self.rolled_back.append(step.id)
        status = "failed" if step.id == self.rollback_fail_on else "success"
        return StepResult(
            id=step.id,
            status=status,
            task_id=f"task-{step.id}" if status == "success" else None,
            message=f"failed at {step.id}" if status == "failed" else "",
        )


class FakeTransport:
    def __init__(self) -> None:
        self.config: dict[str, Any] = {}
        self.cluster_vms = [
            {
                "vmid": 9000,
                "name": "tmpl-ubuntu-cloudinit",
                "node": "pve01",
                "template": 1,
            }
        ]
        self.template_config: dict[str, Any] = {
            "name": "tmpl-ubuntu-cloudinit",
            "template": 1,
            "scsi0": "local-lvm:vm-9000-disk-0,size=20G",
            "ide2": "local-lvm:cloudinit,media=cdrom",
            "agent": "enabled=1",
        }
        self.deleted = False
        self.task_result: dict[str, Any] = {"status": "stopped", "exitstatus": "OK"}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _call(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def nodes(self) -> list[dict[str, Any]]:
        return [{"node": "pve01", "status": "online"}]

    def vms(self) -> list[dict[str, Any]]:
        return self.cluster_vms

    def storages(self, node: str) -> list[dict[str, Any]]:
        return [{"storage": "local-lvm", "avail": 100 * 1024 * 1024 * 1024}]

    def pools(self) -> list[dict[str, Any]]:
        return [{"poolid": "zone-dmz"}]

    def bridges(self, node: str) -> list[dict[str, Any]]:
        return [{"iface": "vmbr0", "bridge_vlan_aware": 1}]

    def permissions(self) -> dict[str, Any]:
        return {
            "/": {
                "Datastore.Audit": 1,
                "Datastore.AllocateSpace": 1,
                "Sys.Audit": 1,
                "VM.Audit": 1,
                "VM.Allocate": 1,
                "VM.Clone": 1,
                "VM.Config.CPU": 1,
                "VM.Config.CDROM": 1,
                "VM.Config.Disk": 1,
                "VM.Config.Memory": 1,
                "VM.Config.Network": 1,
                "VM.Config.Options": 1,
                "VM.PowerMgmt": 1,
            }
        }

    def task_status(self, node: str, upid: str) -> dict[str, Any]:
        return self.task_result

    def clone_vm(self, node: str, template_vmid: int, params: dict[str, Any]) -> str:
        self._call("clone_vm", node, template_vmid, params)
        self.config.update({"name": params["name"], "vmid": params["newid"]})
        return "UPID:clone"

    def create_vm(self, node: str, params: dict[str, Any]) -> str:
        self._call("create_vm", node, params)
        self.config.update({"name": params["name"], "vmid": params["vmid"]})
        return "UPID:create"

    def import_disk(self, node: str, vmid: int, image_path: str, storage: str) -> str:
        self._call("import_disk", node, vmid, image_path, storage)
        self.config["unused0"] = f"{storage}:vm-{vmid}-disk-0"
        return "UPID:import"

    def set_vm_config(
        self,
        node: str,
        vmid: int,
        params: dict[str, Any],
    ) -> str | None:
        self._call("set_vm_config", node, vmid, params)
        self.config.update(params)
        return "UPID:config"

    def resize_disk(self, node: str, vmid: int, disk: str, size: str) -> str | None:
        self._call("resize_disk", node, vmid, disk, size)
        self.config[disk] = size
        return "UPID:resize"

    def start_vm(self, node: str, vmid: int) -> str:
        self.config["status"] = "running"
        return "UPID:start"

    def stop_vm(self, node: str, vmid: int) -> str:
        self.config["status"] = "stopped"
        return "UPID:stop"

    def delete_vm(self, node: str, vmid: int) -> str:
        self.deleted = True
        return "UPID:delete"

    def convert_template(self, node: str, vmid: int) -> str | None:
        self.config["template"] = 1
        return "UPID:template"

    def vm_config(self, node: str, vmid: int) -> dict[str, Any]:
        if vmid == 9000 and not self.config:
            return dict(self.template_config)
        if self.deleted:
            raise KeyError(vmid)
        return dict(self.config)

    def vm_status(self, node: str, vmid: int) -> dict[str, Any]:
        return {"status": self.config.get("status", "running")}

    def guest_agent_network(self, node: str, vmid: int) -> dict[str, Any]:
        return {"result": [{"ip-addresses": [{"ip-address": "192.0.2.21"}]}]}


def write_operation_inputs(
    tmp_path: Path,
) -> tuple[
    Path,
    Path,
    Path,
    ProviderDefinition,
    VmCreateInput,
    VmTemplateCreateInput,
]:
    image = tmp_path / "ubuntu-cloudimg.img"
    image.write_bytes(b"cloud image fixture\n")
    checksum = "sha256:" + hashlib.sha256(image.read_bytes()).hexdigest()

    provider_path = tmp_path / "provider.yml"
    provider_path.write_text(
        yaml.safe_dump(
            {
                "schema": "atlas.provider/v1",
                "provider": "proxmox",
                "safety": {
                    "require_confirm": True,
                    "max_plan_age_seconds": 1800,
                    "allow_rollback_delete": True,
                },
                "connection": {
                    "api_url": "https://pve.example.invalid:8006/api2/json",
                    "verify_ssl": True,
                    "token_id_ref": "env:PROXMOX_TOKEN_ID",
                    "token_secret_ref": "env:PROXMOX_TOKEN_SECRET",
                    "task_timeout_seconds": 1,
                    "poll_interval_seconds": 1,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    vm_input_path = tmp_path / "vm-create.yml"
    vm_input_path.write_text(
        yaml.safe_dump(
            {
                "schema": "atlas.operation-input/v1",
                "kind": "ProxmoxVmCreate",
                "site": "example",
                "target": "web01",
                "create_allowed": True,
                "rollback_delete_allowed": True,
                "vm": {
                    "vmid": 121,
                    "name": "web01",
                    "node": "pve01",
                    "template_vmid": 9000,
                    "template_name": "tmpl-ubuntu-cloudinit",
                    "full_clone": True,
                    "pool": "zone-dmz",
                },
                "resources": {
                    "cores": 2,
                    "sockets": 1,
                    "memory_mb": 2048,
                    "disk": {
                        "device": "scsi0",
                        "size_gb": 20,
                        "storage": "local-lvm",
                    },
                },
                "network": {
                    "bridge": "vmbr0",
                    "vlan": 130,
                    "ip": "192.0.2.21",
                    "prefix": 24,
                    "gateway": "192.0.2.1",
                    "dns_servers": ["192.0.2.53"],
                },
                "cloud_init": {
                    "user": "ops",
                    "ssh_public_keys": [
                        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample"
                    ],
                    "ciupgrade": False,
                },
                "guest": {
                    "qemu_agent": True,
                    "ssh_port": 22,
                },
                "tags": ["provider-proxmox", "zone-dmz", "role-web"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    template_input_path = tmp_path / "vm-template-create.yml"
    template_input_path.write_text(
        yaml.safe_dump(
            {
                "schema": "atlas.operation-input/v1",
                "kind": "ProxmoxVmTemplateCreate",
                "site": "example",
                "target": "tmpl-ubuntu-2404",
                "create_allowed": True,
                "rollback_delete_allowed": True,
                "vmid": 9100,
                "name": "tmpl-ubuntu-2404",
                "node": "pve01",
                "image": {
                    "url": "https://images.example.invalid/ubuntu-cloudimg.img",
                    "checksum": checksum,
                    "shared_path": str(image),
                },
                "resources": {
                    "memory_mb": 1024,
                    "cores": 1,
                    "disk_gb": 10,
                    "storage": "local-lvm",
                    "disk_device": "scsi0",
                },
                "network": {"bridge": "vmbr0", "vlan": 130},
                "cloud_init": {"user": "ops"},
                "guest": {"qemu_agent": True, "serial_console": True},
                "tags": ["provider-proxmox", "os-ubuntu"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    return (
        provider_path,
        vm_input_path,
        template_input_path,
        load_provider_definition(provider_path),
        load_vm_create_input(vm_input_path),
        load_vm_template_create_input(template_input_path),
    )
