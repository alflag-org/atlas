from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from atlas_operations.operation.config import ProxmoxProviderConfig, resolve_secret_ref
from atlas_operations.operation.errors import ProviderError
from atlas_operations.operation.plan import CheckResult, OperationPlan, OperationStep
from atlas_operations.operation.provider import (
    ProviderCapabilities,
    ProviderQuery,
    ProviderState,
    StepResult,
    VerifyResult,
)
from atlas_operations.operation.proxmox.cloudinit import ipconfig0, nameserver
from atlas_operations.operation.proxmox.ownership import (
    marker_matches,
    ownership_marker,
    vm_tags,
)
from atlas_operations.operation.proxmox.tasks import wait_for_task


@dataclass
class ProxmoxApiTransport:
    config: ProxmoxProviderConfig

    def __post_init__(self) -> None:
        self.api = self._connect()

    def _connect(self) -> Any:
        try:
            from proxmoxer import ProxmoxAPI
        except ImportError as exc:
            raise ProviderError("proxmoxer is required for the Proxmox live provider") from exc
        if (
            not self.config.api_url
            or not self.config.token_id_ref
            or not self.config.token_secret_ref
        ):
            raise ProviderError("Proxmox live provider is missing API URL or token refs")
        parsed = urlparse(self.config.api_url)
        token_id = resolve_secret_ref(self.config.token_id_ref)
        token_secret = resolve_secret_ref(self.config.token_secret_ref)
        if "!" not in token_id:
            raise ProviderError("PROXMOX token id must look like user@realm!token-name")
        user, token_name = token_id.split("!", 1)
        try:
            return ProxmoxAPI(
                parsed.hostname,
                user=user,
                token_name=token_name,
                token_value=token_secret,
                verify_ssl=self.config.verify_ssl,
                port=parsed.port or 8006,
                service="PVE",
            )
        except Exception as exc:
            raise ProviderError("failed to initialize Proxmox API client") from exc

    def nodes(self) -> list[dict[str, Any]]:
        return list(self.api.nodes.get())

    def vms(self) -> list[dict[str, Any]]:
        return list(self.api.cluster.resources.get(type="vm"))

    def vm_config(self, node: str, vmid: int) -> dict[str, Any]:
        return dict(self.api.nodes(node).qemu(vmid).config.get())

    def vm_status(self, node: str, vmid: int) -> dict[str, Any]:
        return dict(self.api.nodes(node).qemu(vmid).status.current.get())

    def storages(self, node: str) -> list[dict[str, Any]]:
        return list(self.api.nodes(node).storage.get())

    def pools(self) -> list[dict[str, Any]]:
        return list(self.api.pools.get())

    def bridges(self, node: str) -> list[dict[str, Any]]:
        return list(self.api.nodes(node).network.get(type="bridge"))

    def permissions(self) -> dict[str, Any]:
        return dict(self.api.access.permissions.get())

    def clone_vm(self, node: str, template_vmid: int, params: dict[str, Any]) -> str:
        return str(self.api.nodes(node).qemu(template_vmid).clone.post(**params))

    def set_vm_config(self, node: str, vmid: int, params: dict[str, Any]) -> str | None:
        result = self.api.nodes(node).qemu(vmid).config.put(**params)
        return str(result) if result else None

    def create_vm(self, node: str, params: dict[str, Any]) -> str:
        return str(self.api.nodes(node).qemu.post(**params))

    def import_disk(self, node: str, vmid: int, image_path: str, storage: str) -> str:
        return str(
            self.api.nodes(node)
            .qemu(vmid)
            .importdisk.post(
                filename=image_path,
                storage=storage,
            )
        )

    def resize_disk(self, node: str, vmid: int, disk: str, size: str) -> str | None:
        result = self.api.nodes(node).qemu(vmid).resize.put(disk=disk, size=size)
        return str(result) if result else None

    def start_vm(self, node: str, vmid: int) -> str:
        return str(self.api.nodes(node).qemu(vmid).status.start.post())

    def stop_vm(self, node: str, vmid: int) -> str:
        return str(self.api.nodes(node).qemu(vmid).status.stop.post())

    def delete_vm(self, node: str, vmid: int) -> str:
        return str(self.api.nodes(node).qemu(vmid).delete(purge=1))

    def convert_template(self, node: str, vmid: int) -> str | None:
        result = self.api.nodes(node).qemu(vmid).template.post()
        return str(result) if result else None

    def task_status(self, node: str, upid: str) -> dict[str, Any]:
        return dict(self.api.nodes(node).tasks(upid).status.get())

    def guest_agent_network(self, node: str, vmid: int) -> dict[str, Any]:
        return dict(self.api.nodes(node).qemu(vmid).agent("network-get-interfaces").get())


class ProxmoxProviderClient:
    name = "proxmox"

    def __init__(
        self,
        config: ProxmoxProviderConfig,
        *,
        transport: Any | None = None,
    ) -> None:
        self.config = config
        self.transport = transport if transport is not None else ProxmoxApiTransport(config)
        self._imported_disks: dict[tuple[str, int], str] = {}

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            live_operations=[
                "proxmox.vm-create",
                "proxmox.vm-template-create",
            ],
        )

    def read_state(self, query: ProviderQuery) -> ProviderState:
        if query.kind == "status":
            try:
                nodes = self.transport.nodes()
                vms = self.transport.vms()
            except Exception as exc:
                raise ProviderError("failed to read Proxmox status") from exc
            return ProviderState(
                provider=self.name,
                data={
                    "nodes": nodes,
                    "vms": vms,
                    "node_count": len(nodes),
                    "vm_count": len(vms),
                },
            )
        raise ProviderError(f"unsupported Proxmox query: {query.kind}")

    def preflight(self, plan: OperationPlan) -> VerifyResult:
        if plan.metadata.operation_kind == "proxmox.vm-template-create":
            return self.preflight_template_create(plan)
        checks: list[CheckResult] = [
            CheckResult(name="proxmox.mode", status="passed", message="live"),
        ]
        spec = plan.spec
        node = _node_for(plan)
        vmid = int(spec["vmid"])
        template_vmid = int(spec["templateVmid"])
        disk = _disk_spec(spec)
        try:
            nodes = self.transport.nodes()
            vms = self.transport.vms()
            storages = self.transport.storages(node)
            pools = self.transport.pools()
            bridges = self.transport.bridges(node)
            permissions = self.transport.permissions()
        except Exception as exc:
            checks.append(
                CheckResult(name="proxmox.api", status="failed", message=f"API read failed: {exc}")
            )
            return VerifyResult(status="failed", checks=checks)

        node_names = {str(item.get("node")) for item in nodes}
        online_nodes = {
            str(item.get("node"))
            for item in nodes
            if str(item.get("status", "")).lower() in {"online", "1"}
        }
        checks.append(_check("proxmox.node.exists", node in node_names, node))
        checks.append(_check("proxmox.node.online", node in online_nodes, node))
        checks.append(_check("proxmox.vmid.unused", vmid not in _vmids(vms), str(vmid)))
        checks.append(
            _check("proxmox.name.unused", spec["name"] not in _vm_names(vms), spec["name"])
        )

        template = next((vm for vm in vms if int(vm.get("vmid", -1)) == template_vmid), None)
        checks.append(_check("proxmox.template.exists", template is not None, str(template_vmid)))
        template_config: dict[str, Any] = {}
        if template is not None:
            template_node = str(template.get("node") or node)
            try:
                template_config = self.transport.vm_config(template_node, template_vmid)
            except Exception as exc:
                checks.append(
                    CheckResult(
                        name="proxmox.template.config",
                        status="failed",
                        message=f"template config read failed: {exc}",
                    )
                )
            checks.append(
                _check(
                    "proxmox.template.is-template",
                    bool(template.get("template") in (1, True, "1")),
                    str(template_vmid),
                )
            )
            expected_template_name = spec.get("templateName")
            if expected_template_name:
                actual_template_name = str(
                    template_config.get("name") or template.get("name") or ""
                )
                checks.append(
                    _check(
                        "proxmox.template.name",
                        actual_template_name == expected_template_name,
                        f"{actual_template_name} == {expected_template_name}",
                    )
                )
            checks.append(
                _check(
                    "proxmox.template.cloud-init-drive",
                    _has_cloud_init_drive(template_config),
                    str(template_vmid),
                )
            )
            checks.append(
                _check(
                    "proxmox.template.disk-device",
                    disk["device"] in template_config,
                    disk["device"],
                )
            )
            if spec.get("guest", {}).get("qemuAgent", True):
                checks.append(
                    _check(
                        "proxmox.template.qemu-agent",
                        _agent_enabled(template_config.get("agent")),
                        str(template_config.get("agent")),
                    )
                )

        storage = str(disk["storage"])
        storage_item = next(
            (item for item in storages if str(item.get("storage")) == storage), None
        )
        checks.append(_check("proxmox.storage.exists", storage_item is not None, storage))
        if storage_item is not None:
            required = int(disk["sizeGb"]) * 1024 * 1024 * 1024
            available = int(storage_item.get("avail", 0) or 0)
            checks.append(
                _check("proxmox.storage.free", available >= required, f"{storage} >= {required}")
            )

        pool = spec.get("pool")
        if pool:
            pool_ids = {str(item.get("poolid")) for item in pools}
            checks.append(_check("proxmox.pool.exists", str(pool) in pool_ids, str(pool)))

        bridge = str(spec["network"]["bridge"])
        bridge_item = next((item for item in bridges if str(item.get("iface")) == bridge), None)
        checks.append(_check("proxmox.bridge.exists", bridge_item is not None, bridge))
        if bridge_item is not None and int(spec["network"]["vlan"]) > 0:
            checks.append(
                _check(
                    "proxmox.bridge.vlan-aware",
                    _bridge_allows_vlan(bridge_item),
                    bridge,
                )
            )

        checks.extend(
            _permission_checks(
                permissions,
                node=node,
                storage=storage,
                vmid=vmid,
                template_vmid=template_vmid,
            )
        )

        status = "failed" if any(check.status == "failed" for check in checks) else "passed"
        return VerifyResult(status=status, checks=checks)

    def preflight_template_create(self, plan: OperationPlan) -> VerifyResult:
        checks: list[CheckResult] = [
            CheckResult(name="proxmox.mode", status="passed", message="live"),
        ]
        spec = plan.spec
        node = _node_for(plan)
        vmid = int(spec["vmid"])
        try:
            nodes = self.transport.nodes()
            vms = self.transport.vms()
            storages = self.transport.storages(node)
            bridges = self.transport.bridges(node)
            permissions = self.transport.permissions()
        except Exception as exc:
            checks.append(
                CheckResult(name="proxmox.api", status="failed", message=f"API read failed: {exc}")
            )
            return VerifyResult(status="failed", checks=checks)

        node_names = {str(item.get("node")) for item in nodes}
        online_nodes = {
            str(item.get("node"))
            for item in nodes
            if str(item.get("status", "")).lower() in {"online", "1"}
        }
        checks.append(_check("proxmox.node.exists", node in node_names, node))
        checks.append(_check("proxmox.node.online", node in online_nodes, node))
        checks.append(_check("proxmox.vmid.unused", vmid not in _vmids(vms), str(vmid)))
        checks.append(
            _check("proxmox.name.unused", spec["name"] not in _vm_names(vms), spec["name"])
        )

        storage = str(spec["resources"]["storage"])
        storage_item = next(
            (item for item in storages if str(item.get("storage")) == storage), None
        )
        checks.append(_check("proxmox.storage.exists", storage_item is not None, storage))
        if storage_item is not None:
            required = int(spec["resources"]["diskGb"]) * 1024 * 1024 * 1024
            available = int(storage_item.get("avail", 0) or 0)
            checks.append(
                _check("proxmox.storage.free", available >= required, f"{storage} >= {required}")
            )

        bridge = str(spec["network"]["bridge"])
        bridge_item = next((item for item in bridges if str(item.get("iface")) == bridge), None)
        checks.append(_check("proxmox.bridge.exists", bridge_item is not None, bridge))
        if bridge_item is not None and int(spec["network"]["vlan"]) > 0:
            checks.append(
                _check("proxmox.bridge.vlan-aware", _bridge_allows_vlan(bridge_item), bridge)
            )

        checks.extend(
            _permission_checks(
                permissions,
                node=node,
                storage=storage,
                vmid=vmid,
            )
        )
        checks.append(
            _check(
                "template.image.sha256",
                str(spec["image"]["checksum"]).startswith("sha256:"),
                spec["image"]["checksum"],
            )
        )
        checks.append(
            _check("template.guest.qemu-agent", bool(spec["guest"].get("qemuAgent")), "required")
        )
        checks.append(
            _check(
                "template.guest.serial-console",
                bool(spec["guest"].get("serialConsole")),
                "required",
            )
        )

        status = "failed" if any(check.status == "failed" for check in checks) else "passed"
        return VerifyResult(status=status, checks=checks)

    def apply_step(self, step: OperationStep, plan: OperationPlan) -> StepResult:
        spec = plan.spec
        node = _node_for(plan)
        vmid = int(spec["vmid"])
        try:
            task_id: str | None = None
            if step.action == "clone-template":
                disk = _disk_spec(spec)
                params = {
                    "newid": vmid,
                    "name": spec["name"],
                    "full": 1 if spec.get("fullClone", True) else 0,
                    "target": node,
                }
                if disk.get("storage"):
                    params["storage"] = disk["storage"]
                if spec.get("pool"):
                    params["pool"] = spec["pool"]
                task_id = self.transport.clone_vm(node, int(spec["templateVmid"]), params)
                self._wait_task(node, task_id)
            elif step.action == "template-create-vm":
                task_id = self.transport.create_vm(
                    node,
                    {
                        "vmid": vmid,
                        "name": spec["name"],
                        "memory": int(spec["resources"]["memoryMb"]),
                        "cores": int(spec["resources"]["cores"]),
                        "net0": _template_net0(spec),
                        "ostype": "l26",
                    },
                )
                self._wait_task(node, task_id)
            elif step.action == "template-import-disk":
                image_path = _required_step_param(step, "imagePath")
                before_config = self.transport.vm_config(node, vmid)
                task_id = self.transport.import_disk(
                    node,
                    vmid,
                    image_path,
                    str(spec["resources"]["storage"]),
                )
                self._wait_task(node, task_id)
                after_config = self.transport.vm_config(node, vmid)
                self._imported_disks[(node, vmid)] = _detect_imported_disk(
                    before_config,
                    after_config,
                )
            elif step.action == "template-attach-disk":
                disk_device = str(spec["resources"]["diskDevice"])
                volume = self._imported_disks.get((node, vmid))
                if not volume:
                    volume = _single_unused_disk(self.transport.vm_config(node, vmid))
                task_id = self.transport.set_vm_config(
                    node,
                    vmid,
                    {disk_device: f"{volume},discard=on"},
                )
                self._wait_optional_task(node, task_id)
                task_id = self.transport.resize_disk(
                    node,
                    vmid,
                    disk_device,
                    f"{int(spec['resources']['diskGb'])}G",
                )
                self._wait_optional_task(node, task_id)
            elif step.action == "template-add-cloud-init":
                task_id = self.transport.set_vm_config(
                    node,
                    vmid,
                    {
                        "ide2": f"{spec['resources']['storage']}:cloudinit",
                        "ciuser": spec["cloudInit"]["user"],
                    },
                )
                self._wait_optional_task(node, task_id)
            elif step.action == "template-configure-boot":
                task_id = self.transport.set_vm_config(
                    node,
                    vmid,
                    {
                        "boot": f"order={spec['resources']['diskDevice']}",
                        "scsihw": "virtio-scsi-pci",
                    },
                )
                self._wait_optional_task(node, task_id)
            elif step.action == "template-configure-serial":
                task_id = self.transport.set_vm_config(
                    node,
                    vmid,
                    {"serial0": "socket", "vga": "serial0"},
                )
                self._wait_optional_task(node, task_id)
            elif step.action == "template-enable-agent":
                task_id = self.transport.set_vm_config(node, vmid, {"agent": 1})
                self._wait_optional_task(node, task_id)
            elif step.action == "set-resources":
                task_id = self.transport.set_vm_config(
                    node,
                    vmid,
                    {
                        "cores": int(spec["cores"]),
                        "sockets": int(spec["sockets"]),
                        "memory": int(spec["memoryMb"]),
                    },
                )
                self._wait_optional_task(node, task_id)
            elif step.action == "resize-disk":
                disk = _disk_spec(spec)
                task_id = self.transport.resize_disk(
                    node,
                    vmid,
                    str(disk["device"]),
                    f"{int(disk['sizeGb'])}G",
                )
                self._wait_optional_task(node, task_id)
            elif step.action == "set-network":
                network = spec["network"]
                net0 = f"virtio,bridge={network['bridge']},tag={network['vlan']}"
                task_id = self.transport.set_vm_config(node, vmid, {"net0": net0})
                self._wait_optional_task(node, task_id)
            elif step.action == "set-cloud-init":
                cloud_init = spec["cloudInit"]
                params: dict[str, Any] = {
                    "ciuser": cloud_init["user"],
                    "ciupgrade": 1 if cloud_init.get("ciupgrade") else 0,
                    "ipconfig0": ipconfig0(spec),
                }
                ns = nameserver(spec)
                if ns:
                    params["nameserver"] = ns
                ssh_keys = cloud_init.get("sshPublicKeys") or []
                if ssh_keys:
                    params["sshkeys"] = "\n".join(ssh_keys)
                task_id = self.transport.set_vm_config(node, vmid, params)
                self._wait_optional_task(node, task_id)
            elif step.action == "set-agent":
                task_id = self.transport.set_vm_config(node, vmid, {"agent": 1})
                self._wait_optional_task(node, task_id)
            elif step.action == "set-tags":
                tags = ";".join(spec.get("tags", []))
                task_id = self.transport.set_vm_config(node, vmid, {"tags": tags})
                self._wait_optional_task(node, task_id)
            elif step.action == "set-ownership":
                task_id = self.transport.set_vm_config(
                    node,
                    vmid,
                    {
                        "description": ownership_marker(
                            plan.metadata.plan_id,
                            plan.metadata.operation_kind,
                            plan.metadata.target,
                        )
                    },
                )
                self._wait_optional_task(node, task_id)
            elif step.action == "start-vm":
                task_id = self.transport.start_vm(node, vmid)
                self._wait_task(node, task_id)
            elif step.action == "wait-guest-agent":
                self._wait_guest_agent(node, vmid)
            elif step.action == "wait-ssh":
                self._wait_ssh(spec["network"]["ip"], int(spec["guest"]["sshPort"]))
            elif step.action == "template-convert":
                task_id = self.transport.convert_template(node, vmid)
                self._wait_optional_task(node, task_id)
            else:
                raise ProviderError(f"unsupported Proxmox apply step: {step.action}")
            return StepResult(id=step.id, status="success", task_id=task_id)
        except Exception as exc:
            return StepResult(id=step.id, status="failed", message=str(exc))

    def verify(self, plan: OperationPlan) -> VerifyResult:
        if plan.metadata.operation_kind == "proxmox.vm-template-create":
            return self.verify_template_create(plan)
        spec = plan.spec
        node = _node_for(plan)
        vmid = int(spec["vmid"])
        checks: list[CheckResult] = []
        try:
            config = self.transport.vm_config(node, vmid)
            status = self.transport.vm_status(node, vmid)
        except Exception as exc:
            return VerifyResult(
                status="failed",
                checks=[CheckResult(name="proxmox.vm.exists", status="failed", message=str(exc))],
            )
        checks.append(CheckResult(name="proxmox.vm.exists", status="passed", message=str(vmid)))
        checks.append(
            _check("proxmox.vm.name", str(config.get("name")) == spec["name"], spec["name"])
        )
        checks.append(
            _check(
                "proxmox.vm.running",
                str(status.get("status", "")).lower() == "running",
                str(status.get("status")),
            )
        )
        current_tags = vm_tags(config)
        for tag in spec.get("tags", []):
            checks.append(_check(f"proxmox.tag.{tag}", tag in current_tags, tag))
        checks.append(
            _check(
                "proxmox.ownership-marker",
                marker_matches(
                    config.get("description"),
                    plan.metadata.plan_id,
                    plan.metadata.operation_kind,
                    plan.metadata.target,
                ),
                plan.metadata.plan_id,
            )
        )
        if spec["guest"].get("qemuAgent", True):
            checks.extend(self._guest_agent_checks(node, vmid, spec))
        checks.append(_tcp_check(spec["network"]["ip"], int(spec["guest"]["sshPort"])))
        status_value = "failed" if any(check.status == "failed" for check in checks) else "passed"
        return VerifyResult(status=status_value, checks=checks)

    def verify_template_create(self, plan: OperationPlan) -> VerifyResult:
        spec = plan.spec
        node = _node_for(plan)
        vmid = int(spec["vmid"])
        checks: list[CheckResult] = []
        try:
            config = self.transport.vm_config(node, vmid)
        except Exception as exc:
            return VerifyResult(
                status="failed",
                checks=[
                    CheckResult(name="proxmox.template.exists", status="failed", message=str(exc))
                ],
            )
        checks.append(
            CheckResult(name="proxmox.template.exists", status="passed", message=str(vmid))
        )
        checks.append(
            _check("proxmox.template.name", str(config.get("name")) == spec["name"], spec["name"])
        )
        checks.append(
            _check(
                "proxmox.template.flag",
                config.get("template") in (1, True, "1"),
                str(config.get("template")),
            )
        )
        checks.append(
            _check(
                "proxmox.template.cloud-init-drive",
                _has_cloud_init_drive(config),
                str(vmid),
            )
        )
        checks.append(
            _check(
                "proxmox.template.disk-device",
                str(spec["resources"]["diskDevice"]) in config,
                str(spec["resources"]["diskDevice"]),
            )
        )
        checks.append(
            _check(
                "proxmox.template.qemu-agent",
                _agent_enabled(config.get("agent")),
                str(config.get("agent")),
            )
        )
        current_tags = vm_tags(config)
        for tag in spec.get("tags", []):
            checks.append(_check(f"proxmox.tag.{tag}", tag in current_tags, tag))
        checks.append(
            _check(
                "proxmox.ownership-marker",
                marker_matches(
                    config.get("description"),
                    plan.metadata.plan_id,
                    plan.metadata.operation_kind,
                    plan.metadata.target,
                ),
                plan.metadata.plan_id,
            )
        )
        status_value = "failed" if any(check.status == "failed" for check in checks) else "passed"
        return VerifyResult(status=status_value, checks=checks)

    def rollback_step(self, step: OperationStep, plan: OperationPlan) -> StepResult:
        spec = plan.spec
        node = _node_for(plan)
        vmid = int(spec["vmid"])
        try:
            task_id: str | None = None
            if step.action in {
                "verify-ownership",
                "verify-created-resource",
                "verify-created-template",
            }:
                config = self.transport.vm_config(node, vmid)
                _verify_rollback_live_config(step, plan, config)
                description = str(config.get("description") or "")
                marker_written = bool(step.params.get("evidenceOwnershipMarkerWritten"))
                marker_matched = marker_matches(
                    config.get("description"),
                    plan.metadata.plan_id,
                    plan.metadata.operation_kind,
                    plan.metadata.target,
                )
                if marker_written and not marker_matched:
                    raise ProviderError("ownership marker does not match plan")
                if "managed-by: atlas" in description and not marker_matched:
                    raise ProviderError("ownership marker does not match plan")
            elif step.action == "stop-vm":
                status = self.transport.vm_status(node, vmid)
                if str(status.get("status", "")).lower() == "running":
                    task_id = self.transport.stop_vm(node, vmid)
                    self._wait_task(node, task_id)
            elif step.action == "delete-vm":
                task_id = self.transport.delete_vm(node, vmid)
                self._wait_task(node, task_id)
            elif step.action == "verify-deleted":
                try:
                    self.transport.vm_config(node, vmid)
                except Exception:
                    return StepResult(id=step.id, status="success")
                raise ProviderError("VM still exists after delete")
            else:
                raise ProviderError(f"unsupported Proxmox rollback step: {step.action}")
            return StepResult(id=step.id, status="success", task_id=task_id)
        except Exception as exc:
            return StepResult(id=step.id, status="failed", message=str(exc))

    def _wait_task(self, node: str, task_id: str) -> None:
        wait_for_task(
            self.transport,
            node,
            task_id,
            self.config.task_timeout_seconds,
            self.config.poll_interval_seconds,
        )

    def _wait_optional_task(self, node: str, task_id: str | None) -> None:
        if task_id:
            self._wait_task(node, task_id)

    def _wait_guest_agent(self, node: str, vmid: int) -> None:
        deadline = time.monotonic() + self.config.task_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.transport.guest_agent_network(node, vmid)
                return
            except Exception as exc:
                last_error = exc
                time.sleep(self.config.poll_interval_seconds)
        raise ProviderError(f"guest agent did not respond: {last_error}")

    def _wait_ssh(self, host: str, port: int) -> None:
        deadline = time.monotonic() + self.config.task_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=3):
                    return
            except OSError as exc:
                last_error = exc
                time.sleep(self.config.poll_interval_seconds)
        raise ProviderError(f"SSH TCP port was not reachable: {last_error}")

    def _guest_agent_checks(self, node: str, vmid: int, spec: dict[str, Any]) -> list[CheckResult]:
        try:
            data = self.transport.guest_agent_network(node, vmid)
        except Exception as exc:
            return [CheckResult(name="proxmox.guest-agent", status="failed", message=str(exc))]
        addresses = str(data)
        expected_ip = spec["network"]["ip"]
        return [
            CheckResult(name="proxmox.guest-agent", status="passed", message="responded"),
            _check("proxmox.guest-agent.ip", expected_ip in addresses, expected_ip),
        ]


def _node_for(plan: OperationPlan) -> str:
    node = plan.provider.node or plan.spec.get("node")
    if not node:
        raise ProviderError("plan does not define a Proxmox node")
    return str(node)


def _vmids(vms: list[dict[str, Any]]) -> set[int]:
    result: set[int] = set()
    for vm in vms:
        try:
            result.add(int(vm.get("vmid")))
        except (TypeError, ValueError):
            continue
    return result


def _vm_names(vms: list[dict[str, Any]]) -> set[str]:
    return {str(vm.get("name")) for vm in vms if vm.get("name") is not None}


def _disk_spec(spec: dict[str, Any]) -> dict[str, Any]:
    disk = spec.get("disk")
    if not isinstance(disk, dict):
        raise ProviderError("plan spec does not define disk.device, disk.sizeGb, and disk.storage")
    required = {"device", "sizeGb", "storage"}
    missing = sorted(required - set(disk))
    if missing:
        raise ProviderError(f"plan disk spec is missing: {', '.join(missing)}")
    return disk


def _required_step_param(step: OperationStep, name: str) -> str:
    value = step.params.get(name)
    if not value:
        raise ProviderError(f"step {step.id} requires param {name}")
    return str(value)


def _verify_rollback_live_config(
    step: OperationStep,
    plan: OperationPlan,
    config: dict[str, Any],
) -> None:
    spec = plan.spec
    vmid = int(spec["vmid"])
    evidence_id = _required_step_param(step, "evidenceResourceId")
    evidence_type = _required_step_param(step, "evidenceResourceType")
    evidence_name = _required_step_param(step, "evidenceResourceName")
    evidence_vmid = int(_required_step_param(step, "evidenceResourceVmid"))
    created_by_step = _required_step_param(step, "evidenceCreatedByStep")
    expected_type = (
        "proxmox.qemu-template" if step.action == "verify-created-template" else "proxmox.qemu"
    )
    if evidence_id != f"qemu/{vmid}":
        raise ProviderError("rollback evidence resource id does not match plan")
    if evidence_type != expected_type:
        raise ProviderError("rollback evidence resource type does not match rollback step")
    if evidence_vmid != vmid:
        raise ProviderError("rollback evidence VMID does not match plan")
    if evidence_name != str(spec["name"]):
        raise ProviderError("rollback evidence resource name does not match plan")
    evidence_node = step.params.get("evidenceResourceNode")
    if evidence_node and str(evidence_node) != _node_for(plan):
        raise ProviderError("rollback evidence resource node does not match plan")
    if created_by_step not in {"clone-template", "create-vm"}:
        raise ProviderError("rollback evidence created-by step is not rollback-safe")
    live_vmid = config.get("vmid")
    if live_vmid is not None and int(live_vmid) != vmid:
        raise ProviderError("live VMID does not match plan")
    if str(config.get("name")) != str(spec["name"]):
        raise ProviderError("VM name does not match plan")


def _detect_imported_disk(before_config: dict[str, Any], after_config: dict[str, Any]) -> str:
    before = set(_unused_disks(before_config).values())
    after = set(_unused_disks(after_config).values())
    imported = sorted(after - before)
    if len(imported) != 1:
        raise ProviderError(
            "imported disk could not be determined from Proxmox VM config "
            f"(found {len(imported)} new unused disks)"
        )
    return imported[0]


def _single_unused_disk(config: dict[str, Any]) -> str:
    unused = sorted(_unused_disks(config).values())
    if len(unused) != 1:
        raise ProviderError(f"expected exactly one unused imported disk, found {len(unused)}")
    return unused[0]


def _unused_disks(config: dict[str, Any]) -> dict[str, str]:
    return {
        str(key): str(value).split(",", 1)[0]
        for key, value in config.items()
        if str(key).startswith("unused") and value
    }


def _template_net0(spec: dict[str, Any]) -> str:
    network = spec["network"]
    return f"virtio,bridge={network['bridge']},tag={network['vlan']}"


def _has_cloud_init_drive(config: dict[str, Any]) -> bool:
    return any("cloudinit" in str(value).lower() for value in config.values())


def _agent_enabled(value: Any) -> bool:
    if value in (1, True):
        return True
    normalized = str(value or "").lower()
    return normalized == "1" or "enabled=1" in normalized


def _bridge_allows_vlan(bridge: dict[str, Any]) -> bool:
    value = bridge.get("bridge_vlan_aware")
    if value in (1, True):
        return True
    return str(value).lower() in {"1", "yes", "true", "on"}


def _permission_checks(
    permissions: dict[str, Any],
    *,
    node: str,
    storage: str,
    vmid: int,
    template_vmid: int | None = None,
) -> list[CheckResult]:
    grants = _permission_map(permissions)
    node_path = f"/nodes/{node}"
    storage_path = f"/storage/{storage}"
    target_vm_path = f"/vms/{vmid}"
    read_requirements = [
        ("Sys.Audit", node_path),
        ("Datastore.Audit", storage_path),
        ("VM.Audit", target_vm_path),
    ]
    write_requirements = [
        ("Datastore.AllocateSpace", storage_path),
        ("VM.Allocate", target_vm_path),
        ("VM.Config.CPU", target_vm_path),
        ("VM.Config.CDROM", target_vm_path),
        ("VM.Config.Disk", target_vm_path),
        ("VM.Config.Memory", target_vm_path),
        ("VM.Config.Network", target_vm_path),
        ("VM.Config.Options", target_vm_path),
        ("VM.PowerMgmt", target_vm_path),
    ]
    if template_vmid is not None:
        source_template_path = f"/vms/{template_vmid}"
        read_requirements.append(("VM.Audit", source_template_path))
        write_requirements.append(("VM.Clone", source_template_path))

    return [
        _permission_check("proxmox.permissions.read", grants, read_requirements),
        _permission_check("proxmox.permissions.write", grants, write_requirements),
    ]


def _permission_check(
    name: str,
    grants: dict[str, set[str]],
    requirements: list[tuple[str, str]],
) -> CheckResult:
    missing = [
        f"{capability}@{path}"
        for capability, path in requirements
        if not _has_permission(grants, capability, path)
    ]
    return CheckResult(
        name=name,
        status="failed" if missing else "passed",
        message=", ".join(missing) if missing else "ok",
        details={"missing": missing},
    )


def _permission_map(permissions: dict[str, Any]) -> dict[str, set[str]]:
    grants: dict[str, set[str]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if isinstance(nested, dict) and str(key).startswith("/"):
                    grants[str(key)] = {
                        str(capability)
                        for capability, enabled in nested.items()
                        if "." in str(capability) and enabled
                    }
                    visit(nested)
                else:
                    visit(nested)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(permissions)
    return grants


def _has_permission(grants: dict[str, set[str]], capability: str, path: str) -> bool:
    return any(
        capability in capabilities and _permission_path_applies(grant_path, path)
        for grant_path, capabilities in grants.items()
    )


def _permission_path_applies(grant_path: str, requested_path: str) -> bool:
    normalized = grant_path.rstrip("/") or "/"
    if normalized == "/":
        return True
    return requested_path == normalized or requested_path.startswith(f"{normalized}/")


def _check(name: str, passed: bool, message: str) -> CheckResult:
    return CheckResult(
        name=name,
        status="passed" if passed else "failed",
        message=message,
    )


def _tcp_check(host: str, port: int) -> CheckResult:
    try:
        with socket.create_connection((host, port), timeout=3):
            return CheckResult(name="ssh.tcp", status="passed", message=f"{host}:{port}")
    except OSError as exc:
        return CheckResult(name="ssh.tcp", status="failed", message=str(exc))
