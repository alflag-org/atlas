from __future__ import annotations

from typing import Any


def ownership_marker(plan_id: str, operation_kind: str, host: str) -> str:
    return "\n".join(
        [
            "managed-by: atlas",
            f"atlas-plan-id: {plan_id}",
            f"atlas-operation-kind: {operation_kind}",
            f"atlas-target: {host}",
        ]
    )


def marker_matches(description: str | None, plan_id: str, operation_kind: str, host: str) -> bool:
    if not description:
        return False
    expected = ownership_marker(plan_id, operation_kind, host).splitlines()
    return all(line in description.splitlines() for line in expected)


def vm_tags(config: dict[str, Any]) -> set[str]:
    raw = config.get("tags") or ""
    if isinstance(raw, list):
        return {str(item) for item in raw}
    return {tag for tag in str(raw).replace(",", ";").split(";") if tag}
