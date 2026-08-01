"""Strict public models for managed host lifecycle operations."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from atlas_host_operations.lifecycle import (
    PROVISIONING_PHASES,
    ProvisioningPhase,
    ResourceLifecycle,
    StepStatus,
)

HOST_API_VERSION = "atlas.host-operation/v1"
HOST_SPEC_VERSION = "atlas.host-spec/v1"
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
HOST_NAME_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
CONFIG_NAME_PATTERN = r"^[a-z][a-z0-9_-]*$"
SSH_USER_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$"


class HostModel(BaseModel):
    """Strict base model shared by host input and public artifacts."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class HostResourceSpec(HostModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, pattern=HOST_NAME_PATTERN)
    site: str = Field(min_length=1, pattern=CONFIG_NAME_PATTERN)
    zone: str = Field(min_length=1, pattern=CONFIG_NAME_PATTERN)


class RegistrySpec(HostModel):
    profile: str = Field(min_length=1)


class ProviderSpec(HostModel):
    adapter: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    input: str = Field(min_length=1)


class ConfigurationSpec(HostModel):
    adapter: str = Field(min_length=1)
    project_root: str = Field(min_length=1)
    target: str = Field(min_length=1, pattern=HOST_NAME_PATTERN)
    bootstrap_playbook: str = Field(min_length=1, pattern=CONFIG_NAME_PATTERN)
    converge_playbook: str = Field(min_length=1, pattern=CONFIG_NAME_PATTERN)


class ReadinessSpec(HostModel):
    address: str = Field(min_length=1)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(min_length=1, pattern=SSH_USER_PATTERN)
    require_cloud_init: bool = True
    require_guest_agent: bool = True


class HostSpec(HostModel):
    schema_version: Literal["atlas.host-spec/v1"] = Field(alias="schema")
    kind: Literal["HostCreate"]
    resource: HostResourceSpec
    registry: RegistrySpec
    provider: ProviderSpec
    configuration: ConfigurationSpec
    readiness: ReadinessSpec

    @model_validator(mode="after")
    def require_matching_target(self) -> HostSpec:
        if self.configuration.target != self.resource.name:
            raise ValueError("configuration target must equal resource name")
        return self


class SourceReference(HostModel):
    path: str
    digest: str

    @field_validator("path")
    @classmethod
    def require_absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("source path must be absolute")
        return value

    @field_validator("digest")
    @classmethod
    def require_sha256(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("source digest must use sha256:<64-hex-digest>")
        return value


class GitSourceReference(HostModel):
    path: str
    git_commit: str = Field(alias="gitCommit")
    git_dirty: bool = Field(alias="gitDirty")

    @field_validator("path")
    @classmethod
    def require_absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("project path must be absolute")
        return value

    @field_validator("git_commit")
    @classmethod
    def require_commit(cls, value: str) -> str:
        if not GIT_COMMIT_RE.fullmatch(value):
            raise ValueError("gitCommit must be a full Git object ID")
        return value


class HostPlanSources(HostModel):
    host_spec: SourceReference = Field(alias="hostSpec")
    registry_profile: SourceReference = Field(alias="registryProfile")
    provider_definition: SourceReference = Field(alias="providerDefinition")
    provider_input: SourceReference = Field(alias="providerInput")
    provisioning_project: GitSourceReference = Field(alias="provisioningProject")


class HostPlanMetadata(HostModel):
    plan_id: str = Field(alias="planId", min_length=1)
    operation_kind: Literal["HostCreate"] = Field(alias="operationKind")
    created_at: datetime = Field(alias="createdAt")
    target: str = Field(min_length=1, pattern=HOST_NAME_PATTERN)
    site: str = Field(min_length=1, pattern=CONFIG_NAME_PATTERN)
    risk: Literal["high"] = "high"
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1)
    fingerprint: str | None = None

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("createdAt must include a timezone")
        return value

    @field_validator("fingerprint")
    @classmethod
    def require_fingerprint(cls, value: str | None) -> str | None:
        if value is not None and not SHA256_RE.fullmatch(value):
            raise ValueError("fingerprint must use sha256:<64-hex-digest>")
        return value


class HostPlanResource(HostModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, pattern=HOST_NAME_PATTERN)
    lifecycle_before: Literal[ResourceLifecycle.ABSENT] = Field(alias="lifecycleBefore")
    lifecycle_after: Literal[ResourceLifecycle.ACTIVE] = Field(alias="lifecycleAfter")
    registry_revision: int = Field(alias="registryRevision", ge=1)


class HostPlanProvider(HostModel):
    adapter: str = Field(min_length=1)
    resource_type: str = Field(alias="resourceType", min_length=1)
    plan: dict[str, Any] = Field(default_factory=dict)


class HostPlanConfiguration(HostModel):
    adapter: str = Field(min_length=1)
    target: str = Field(min_length=1, pattern=HOST_NAME_PATTERN)
    bootstrap_playbook: str = Field(
        alias="bootstrapPlaybook",
        min_length=1,
        pattern=CONFIG_NAME_PATTERN,
    )
    converge_playbook: str = Field(
        alias="convergePlaybook",
        min_length=1,
        pattern=CONFIG_NAME_PATTERN,
    )


class HostOperationPlan(HostModel):
    api_version: Literal["atlas.host-operation/v1"] = Field(alias="apiVersion")
    kind: Literal["HostOperationPlan"]
    metadata: HostPlanMetadata
    resource: HostPlanResource
    sources: HostPlanSources
    provider: HostPlanProvider
    configuration: HostPlanConfiguration
    readiness: ReadinessSpec
    phases: list[ProvisioningPhase]

    @model_validator(mode="after")
    def require_consistent_plan(self) -> HostOperationPlan:
        if tuple(self.phases) != PROVISIONING_PHASES:
            raise ValueError("phases must use the complete provisioning order")
        if self.metadata.target != self.resource.name:
            raise ValueError("plan target must equal resource name")
        if self.configuration.target != self.resource.name:
            raise ValueError("configuration target must equal resource name")
        return self

    def as_artifact(self, *, exclude_none: bool = True) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=exclude_none)


class CheckResult(HostModel):
    name: str = Field(min_length=1)
    status: Literal["passed", "failed", "warning", "skipped"]
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class RegistryAuthority(HostModel):
    operation_id: str = Field(alias="operationId", min_length=1)
    lock_scope: str = Field(alias="lockScope", min_length=3)
    fencing_token: int = Field(alias="fencingToken", ge=1)
    operation_revision: int = Field(alias="operationRevision", ge=1)
    resource_revision: int = Field(alias="resourceRevision", ge=1)


class ProviderEvidence(HostModel):
    provider: str = Field(min_length=1)
    resource_type: str = Field(alias="resourceType", min_length=1)
    resource_id: str = Field(alias="resourceId", min_length=1)
    resource_name: str = Field(alias="resourceName", min_length=1)
    locator: dict[str, Any] = Field(default_factory=dict)
    ownership_marker: bool = Field(alias="ownershipMarker")
    details: dict[str, Any] = Field(default_factory=dict)


class ProviderObservation(HostModel):
    exists: bool
    running: bool
    guest_agent_ready: bool = Field(alias="guestAgentReady")
    addresses: list[str] = Field(default_factory=list)
    absence_confirmed: bool = Field(default=False, alias="absenceConfirmed")
    provider_evidence: ProviderEvidence | None = Field(
        default=None,
        alias="providerEvidence",
    )
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(HostModel):
    status: Literal["passed", "failed", "warning"]
    checks: list[CheckResult] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class StepResult(HostModel):
    status: Literal["succeeded", "failed"]
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class RollbackResult(HostModel):
    status: Literal["succeeded", "failed", "skipped"]
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class HostOperationEvidence(HostModel):
    api_version: Literal["atlas.host-operation/v1"] = Field(alias="apiVersion")
    kind: Literal["HostOperationEvidence"]
    operation_id: str = Field(alias="operationId", min_length=1)
    plan_id: str = Field(alias="planId", min_length=1)
    resource_id: str = Field(alias="resourceId", min_length=1)
    phase: ProvisioningPhase
    status: StepStatus
    started_at: datetime = Field(alias="startedAt")
    finished_at: datetime = Field(alias="finishedAt")
    attempt: int = Field(ge=1)
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("evidence timestamps must include a timezone")
        return value

    def as_artifact(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class RegistryResource(HostModel):
    id: str = Field(min_length=1)
    key: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    lifecycle_state: str = Field(alias="lifecycleState", min_length=1)
    revision: int = Field(ge=1)
    binding: dict[str, Any] | None = None


class RegistryStep(HostModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    status: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)
    revision: int = Field(ge=1)


class RegistryOperation(HostModel):
    id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    revision: int = Field(ge=1)
    plan: dict[str, Any] = Field(default_factory=dict)
    resources: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[RegistryStep] = Field(default_factory=list)


class HostStatus(HostModel):
    operation_id: str | None = Field(alias="operationId")
    plan_id: str = Field(alias="planId", min_length=1)
    resource_id: str = Field(alias="resourceId", min_length=1)
    operation_status: str = Field(alias="operationStatus", min_length=1)
    resource_lifecycle: str = Field(alias="resourceLifecycle", min_length=1)
    current_phase: str | None = Field(alias="currentPhase")
    steps: list[dict[str, Any]] = Field(default_factory=list)

    def as_artifact(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)
