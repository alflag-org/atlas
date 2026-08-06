"""Strict models for reviewed operation plans."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

API_VERSION = "atlas.operation/v1"
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class OperationModel(BaseModel):
    """Strict base model shared by operation artifacts."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class CheckResult(OperationModel):
    name: str
    status: Literal["passed", "failed", "warning", "skipped"]
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class PlanMetadata(OperationModel):
    plan_id: str = Field(alias="planId", min_length=1)
    created_at: datetime = Field(alias="createdAt")
    operation_kind: str = Field(alias="operationKind", min_length=1)
    target: str = Field(min_length=1)
    site: str = Field(min_length=1)
    risk: Literal["low", "medium", "high", "critical"] = "medium"
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
    def validate_fingerprint_format(cls, value: str | None) -> str | None:
        if value is not None and not SHA256_RE.fullmatch(value):
            raise ValueError("fingerprint must use sha256:<64-hex-digest>")
        return value


class SourceReference(OperationModel):
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
    def validate_digest(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("source digest must use sha256:<64-hex-digest>")
        return value


class PlanSource(OperationModel):
    input: SourceReference
    provider: SourceReference


class SafetyPolicy(OperationModel):
    requires_confirm: bool = Field(default=True, alias="requiresConfirm")
    requires_rollback: bool = Field(default=True, alias="requiresRollback")
    max_plan_age_seconds: int = Field(default=1800, alias="maxPlanAgeSeconds")
    allowed_only_if_preflight_passes: bool = Field(
        default=True,
        alias="allowedOnlyIfPreflightPasses",
    )


class ProviderReference(OperationModel):
    name: Literal["proxmox"]
    mode: Literal["live"]
    node: str = Field(min_length=1)


class PreflightResult(OperationModel):
    status: Literal["passed", "failed", "warning"]
    checks: list[CheckResult] = Field(default_factory=list)


class OperationStep(OperationModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    action: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class OperationPhase(OperationModel):
    steps: list[OperationStep] = Field(default_factory=list)


class RollbackPhase(OperationPhase):
    supported: bool = False
    delete_only_if_created_by_this_plan: bool = Field(
        default=True,
        alias="deleteOnlyIfCreatedByThisPlan",
    )


class OperationPlan(OperationModel):
    api_version: Literal["atlas.operation/v1"] = Field(alias="apiVersion")
    kind: Literal["OperationPlan"]
    metadata: PlanMetadata
    source: PlanSource
    safety: SafetyPolicy
    provider: ProviderReference
    spec: dict[str, Any]
    preflight: PreflightResult
    changes: list[dict[str, Any]] = Field(default_factory=list)
    apply: OperationPhase = Field(default_factory=OperationPhase)
    verify: OperationPhase = Field(default_factory=OperationPhase)
    rollback: RollbackPhase = Field(default_factory=RollbackPhase)

    def as_artifact(self, *, exclude_none: bool = True) -> dict[str, Any]:
        """Return the public camel-case JSON representation."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=exclude_none)
