"""Strict evidence models for reviewed operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from atlas_operations.operation.plan import SHA256_RE, CheckResult, OperationModel


class EvidenceMetadata(OperationModel):
    evidence_id: str = Field(alias="evidenceId", min_length=1)
    plan_id: str = Field(alias="planId", min_length=1)
    created_at: datetime = Field(alias="createdAt")
    operation_kind: str = Field(alias="operationKind", min_length=1)
    target: str = Field(min_length=1)
    result: Literal["success", "failed"]

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("createdAt must include a timezone")
        return value


class EvidenceProvider(OperationModel):
    name: str
    node: str | None = None
    vmid: int | None = None


class EvidenceResource(OperationModel):
    provider: str
    type: str
    id: str
    name: str
    node: str | None = None
    vmid: int | None = None
    created_by_step: str = Field(alias="createdByStep")
    ownership_marker_written: bool = Field(default=False, alias="ownershipMarkerWritten")


class EvidenceStep(OperationModel):
    id: str
    status: Literal["success", "failed", "skipped"]
    task_id: str | None = Field(default=None, alias="taskId")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    finished_at: datetime | None = Field(default=None, alias="finishedAt")
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class EvidenceVerify(OperationModel):
    status: Literal["passed", "failed", "warning"]
    checks: list[CheckResult] = Field(default_factory=list)


class EvidenceRollback(OperationModel):
    supported: bool = False
    result: Literal["success", "failed"] | None = None


class EvidencePlanSnapshot(OperationModel):
    fingerprint: str
    snapshot: dict[str, Any]

    @field_validator("fingerprint")
    @classmethod
    def validate_fingerprint_format(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("fingerprint must use sha256:<64-hex-digest>")
        return value


class OperationEvidence(OperationModel):
    api_version: Literal["atlas.operation/v1"] = Field(alias="apiVersion")
    kind: Literal["OperationEvidence"]
    metadata: EvidenceMetadata
    plan: EvidencePlanSnapshot
    provider: EvidenceProvider
    created_resources: list[EvidenceResource] = Field(
        default_factory=list,
        alias="createdResources",
    )
    steps: list[EvidenceStep] = Field(default_factory=list)
    verify: EvidenceVerify | None = None
    rollback: EvidenceRollback = Field(default_factory=EvidenceRollback)

    def as_artifact(self, *, exclude_none: bool = True) -> dict[str, Any]:
        """Return the public camel-case JSON representation."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=exclude_none)
