from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from atlas_operations.operation.errors import PlanError
from atlas_operations.operation.plan import OperationPlan


def canonical_plan_payload(plan_or_data: OperationPlan | dict[str, Any]) -> dict[str, Any]:
    if isinstance(plan_or_data, OperationPlan):
        payload = plan_or_data.as_artifact(exclude_none=True)
    else:
        payload = copy.deepcopy(plan_or_data)
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise PlanError("plan metadata is missing")
    metadata.pop("fingerprint", None)
    return payload


def calculate_fingerprint(plan_or_data: OperationPlan | dict[str, Any]) -> str:
    payload = canonical_plan_payload(plan_or_data)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def set_fingerprint(plan: OperationPlan) -> OperationPlan:
    data = plan.as_artifact(exclude_none=True)
    data["metadata"]["fingerprint"] = calculate_fingerprint(data)
    return OperationPlan.model_validate(data)


def validate_fingerprint(plan: OperationPlan) -> None:
    expected = calculate_fingerprint(plan)
    if plan.metadata.fingerprint != expected:
        raise PlanError(
            "plan fingerprint is invalid"
            if plan.metadata.fingerprint
            else "plan fingerprint is missing"
        )
