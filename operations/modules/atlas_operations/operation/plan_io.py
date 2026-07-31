"""Read-only plan artifact loading."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from atlas_operations.operation.errors import PlanError
from atlas_operations.operation.io import read_json
from atlas_operations.operation.plan import OperationPlan


def load_plan(path: str | Path) -> OperationPlan:
    """Load one strict operation plan."""
    try:
        return OperationPlan.model_validate(
            read_json(path),
            by_alias=True,
            by_name=False,
        )
    except ValidationError as exc:
        raise PlanError(f"invalid operation plan: {exc}") from exc
