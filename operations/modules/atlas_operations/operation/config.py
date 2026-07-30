"""Explicit provider definitions and operation input models."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from atlas_operations.operation.errors import InputError
from atlas_operations.operation.io import read_yaml

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOST_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SECRET_REF_PREFIXES = ("env:", "file:")
_PLAINTEXT_SECRET_KEYS = {
    "api_token",
    "password",
    "secret",
    "token",
    "token_secret",
}
_Model = TypeVar("_Model", bound=BaseModel)


class StrictInputModel(BaseModel):
    """Base model for fail-closed external operation input."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


class SafetyConfig(StrictInputModel):
    require_confirm: Literal[True] = True
    max_plan_age_seconds: int = Field(default=1800, gt=0)
    allow_rollback_delete: bool = True


class ProxmoxProviderConfig(StrictInputModel):
    api_url: str
    verify_ssl: bool = True
    token_id_ref: str
    token_secret_ref: str
    task_timeout_seconds: int = Field(default=900, gt=0)
    poll_interval_seconds: int = Field(default=3, gt=0)

    @field_validator("api_url")
    @classmethod
    def validate_api_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("api_url must use https://")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("api_url must not include credentials, query, or fragment")
        return value.rstrip("/")

    @field_validator("token_id_ref", "token_secret_ref")
    @classmethod
    def validate_secret_reference(cls, value: str) -> str:
        if not is_secret_ref(value):
            raise ValueError("secret values must use an env: or file: reference")
        return value


class ProviderDefinition(StrictInputModel):
    schema_version: Literal["atlas.provider/v1"] = Field(alias="schema")
    provider: Literal["proxmox"]
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    connection: ProxmoxProviderConfig


class VmSpec(StrictInputModel):
    vmid: int = Field(gt=0)
    name: str
    node: str = Field(min_length=1)
    template_vmid: int = Field(gt=0)
    template_name: str = Field(min_length=1)
    full_clone: bool = True
    pool: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _HOST_NAME_RE.fullmatch(value):
            raise ValueError("name must be a lowercase DNS label")
        return value


class DiskSpec(StrictInputModel):
    device: str = Field(min_length=1)
    size_gb: int = Field(gt=0)
    storage: str = Field(min_length=1)


class VmResources(StrictInputModel):
    cores: int = Field(default=1, gt=0)
    sockets: int = Field(default=1, gt=0)
    memory_mb: int = Field(gt=0)
    disk: DiskSpec


class VmNetwork(StrictInputModel):
    bridge: str = Field(min_length=1)
    vlan: int = Field(ge=1, le=4094)
    ip: str = Field(min_length=1)
    prefix: int = Field(ge=1, le=32)
    gateway: str = Field(min_length=1)
    dns_servers: list[str] = Field(default_factory=list)


class VmCloudInit(StrictInputModel):
    user: str = Field(min_length=1)
    ssh_public_keys: list[str] = Field(default_factory=list)
    ciupgrade: bool = False


class VmGuest(StrictInputModel):
    qemu_agent: Literal[True] = True
    ssh_port: int = Field(default=22, ge=1, le=65535)


class VmCreateInput(StrictInputModel):
    schema_version: Literal["atlas.operation-input/v1"] = Field(alias="schema")
    kind: Literal["ProxmoxVmCreate"]
    site: str = Field(min_length=1)
    target: str = Field(min_length=1)
    create_allowed: bool = False
    rollback_delete_allowed: bool = False
    vm: VmSpec
    resources: VmResources
    network: VmNetwork
    cloud_init: VmCloudInit
    guest: VmGuest
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_target(self) -> VmCreateInput:
        if self.target != self.vm.name:
            raise ValueError("target must equal vm.name")
        return self

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        if any(not _TAG_RE.fullmatch(value) for value in values):
            raise ValueError("tags must contain lowercase letters, digits, and hyphens")
        return values

    def to_plan_spec(self) -> dict[str, Any]:
        tags = list(dict.fromkeys([*self.tags, "managed-atlas", "platform-vm"]))
        return {
            "policy": {
                "createAllowed": self.create_allowed,
                "rollbackDeleteAllowed": self.rollback_delete_allowed,
            },
            "vmid": self.vm.vmid,
            "name": self.vm.name,
            "templateVmid": self.vm.template_vmid,
            "templateName": self.vm.template_name,
            "fullClone": self.vm.full_clone,
            "pool": self.vm.pool,
            "cores": self.resources.cores,
            "sockets": self.resources.sockets,
            "memoryMb": self.resources.memory_mb,
            "disk": {
                "device": self.resources.disk.device,
                "sizeGb": self.resources.disk.size_gb,
                "storage": self.resources.disk.storage,
            },
            "network": {
                "bridge": self.network.bridge,
                "vlan": self.network.vlan,
                "ip": self.network.ip,
                "prefix": self.network.prefix,
                "gateway": self.network.gateway,
                "dnsServers": list(self.network.dns_servers),
            },
            "cloudInit": {
                "user": self.cloud_init.user,
                "sshPublicKeys": list(self.cloud_init.ssh_public_keys),
                "ciupgrade": self.cloud_init.ciupgrade,
            },
            "guest": {
                "qemuAgent": self.guest.qemu_agent,
                "sshPort": self.guest.ssh_port,
            },
            "tags": tags,
        }


class TemplateImage(StrictInputModel):
    url: str
    checksum: str
    shared_path: Path | None = None
    runner_path: Path | None = None
    node_path: Path | None = None

    @field_validator("checksum")
    @classmethod
    def validate_checksum(cls, value: str) -> str:
        normalized = value.lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", normalized):
            raise ValueError("checksum must use sha256:<64-hex-digest>")
        return normalized

    @model_validator(mode="after")
    def validate_transfer_paths(self) -> TemplateImage:
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("image.url must use https://")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "image.url must not include credentials, query, or fragment"
            )
        paths = [
            path
            for path in (self.shared_path, self.runner_path, self.node_path)
            if path is not None
        ]
        if any(not path.is_absolute() for path in paths):
            raise ValueError("image transfer paths must be absolute")
        if self.shared_path is not None:
            if self.runner_path is not None or self.node_path is not None:
                raise ValueError(
                    "shared_path cannot be combined with runner_path or node_path"
                )
            return self
        if self.runner_path is None or self.node_path is None:
            raise ValueError(
                "image requires shared_path, or both runner_path and node_path"
            )
        return self


class TemplateResources(StrictInputModel):
    memory_mb: int = Field(gt=0)
    cores: int = Field(default=1, gt=0)
    disk_gb: int = Field(gt=0)
    storage: str = Field(min_length=1)
    disk_device: str = Field(min_length=1)


class TemplateNetwork(StrictInputModel):
    bridge: str = Field(min_length=1)
    vlan: int = Field(ge=1, le=4094)


class TemplateCloudInit(StrictInputModel):
    user: str = Field(min_length=1)


class TemplateGuest(StrictInputModel):
    qemu_agent: Literal[True] = True
    serial_console: Literal[True] = True


class VmTemplateCreateInput(StrictInputModel):
    schema_version: Literal["atlas.operation-input/v1"] = Field(alias="schema")
    kind: Literal["ProxmoxVmTemplateCreate"]
    site: str = Field(min_length=1)
    target: str = Field(min_length=1)
    create_allowed: bool = False
    rollback_delete_allowed: bool = False
    vmid: int = Field(gt=0)
    name: str = Field(min_length=1)
    node: str = Field(min_length=1)
    image: TemplateImage
    resources: TemplateResources
    network: TemplateNetwork
    cloud_init: TemplateCloudInit
    guest: TemplateGuest = Field(default_factory=TemplateGuest)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_target(self) -> VmTemplateCreateInput:
        if self.target != self.name:
            raise ValueError("target must equal name")
        if not _HOST_NAME_RE.fullmatch(self.name):
            raise ValueError("name must be a lowercase DNS label")
        return self

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        if any(not _TAG_RE.fullmatch(value) for value in values):
            raise ValueError("tags must contain lowercase letters, digits, and hyphens")
        return values

    def to_plan_spec(self) -> dict[str, Any]:
        runner_path = self.image.runner_path or self.image.shared_path
        node_path = self.image.node_path or self.image.shared_path
        tags = list(dict.fromkeys([*self.tags, "managed-atlas", "platform-template"]))
        return {
            "policy": {
                "createAllowed": self.create_allowed,
                "rollbackDeleteAllowed": self.rollback_delete_allowed,
            },
            "vmid": self.vmid,
            "name": self.name,
            "node": self.node,
            "image": {
                "url": self.image.url,
                "checksum": self.image.checksum,
                "transfer": {
                    "mode": "shared-path",
                    "runnerPath": str(runner_path),
                    "nodePath": str(node_path),
                },
            },
            "resources": {
                "memoryMb": self.resources.memory_mb,
                "cores": self.resources.cores,
                "diskGb": self.resources.disk_gb,
                "storage": self.resources.storage,
                "diskDevice": self.resources.disk_device,
            },
            "network": {
                "bridge": self.network.bridge,
                "vlan": self.network.vlan,
            },
            "cloudInit": {"user": self.cloud_init.user},
            "guest": {
                "qemuAgent": self.guest.qemu_agent,
                "serialConsole": self.guest.serial_console,
            },
            "tags": tags,
        }


def is_secret_ref(value: str) -> bool:
    """Return whether a value names an environment or file secret."""
    return value.startswith(_SECRET_REF_PREFIXES)


def resolve_secret_ref(value: str) -> str:
    """Resolve one explicit secret reference without persisting its value."""
    if value.startswith("env:"):
        name = value.removeprefix("env:")
        if not _ENV_NAME_RE.fullmatch(name):
            raise InputError(f"invalid environment secret name: {name}")
        secret = os.environ.get(name)
        if not secret:
            raise InputError(f"required environment secret {name} is not set")
        return secret
    if value.startswith("file:"):
        path = Path(value.removeprefix("file:"))
        if not path.is_absolute():
            raise InputError(f"secret file path must be absolute: {path}")
        if not path.is_file() or path.is_symlink():
            raise InputError(f"required secret file is missing or unsafe: {path}")
        secret = path.read_text(encoding="utf-8").strip()
        if not secret:
            raise InputError(f"required secret file is empty: {path}")
        return secret
    raise InputError(f"unsupported secret reference: {value}")


def load_provider_definition(path: str | Path) -> ProviderDefinition:
    return _load_model(path, ProviderDefinition)


def load_vm_create_input(path: str | Path) -> VmCreateInput:
    return _load_model(path, VmCreateInput)


def load_vm_template_create_input(path: str | Path) -> VmTemplateCreateInput:
    return _load_model(path, VmTemplateCreateInput)


def _load_model(path: str | Path, model: type[_Model]) -> _Model:
    raw = read_yaml(path)
    reject_plaintext_secrets(raw)
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise InputError(f"invalid {Path(path)}: {exc}") from exc


def reject_plaintext_secrets(value: Any, path: tuple[str, ...] = ()) -> None:
    """Reject secret-looking keys unless their value is an explicit reference."""
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = (*path, key_text)
            if key_text.lower() in _PLAINTEXT_SECRET_KEYS:
                if not isinstance(child, str) or not is_secret_ref(child):
                    raise InputError(
                        f"plaintext secret is not allowed at {'.'.join(child_path)}"
                    )
            reject_plaintext_secrets(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_plaintext_secrets(child, (*path, str(index)))
