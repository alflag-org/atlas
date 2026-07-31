from __future__ import annotations

import time
from typing import Any

from atlas_operations.operation.errors import ProviderError


def wait_for_task(transport: Any, node: str, upid: str, timeout_seconds: int, interval: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_status: dict | None = None
    while time.monotonic() < deadline:
        last_status = transport.task_status(node, upid)
        status = str(last_status.get("status", "")).lower()
        if status == "stopped":
            exit_status = last_status.get("exitstatus")
            if exit_status in (None, "OK"):
                return last_status
            raise ProviderError(f"Proxmox task failed: {upid}: {exit_status}")
        time.sleep(interval)
    raise ProviderError(f"Proxmox task timed out: {upid}: last_status={last_status}")
