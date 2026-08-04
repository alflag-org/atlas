"""Read-only evidence artifact loading."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from atlas_operations.operation.errors import PlanError
from atlas_operations.operation.evidence import OperationEvidence
from atlas_operations.operation.io import read_json


def load_evidence(path: str | Path) -> OperationEvidence:
    """Load one strict operation evidence artifact."""
    try:
        return OperationEvidence.model_validate(
            read_json(path),
            by_alias=True,
            by_name=False,
        )
    except ValidationError as exc:
        raise PlanError(f"invalid operation evidence: {exc}") from exc
