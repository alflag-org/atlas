"""Read one durable operation through the configured Global Registry profile."""

from __future__ import annotations

import argparse
import os
import sys

from atlas_host_operations.artifacts import write_json
from atlas_host_operations.controller import _exception_exit_code
from atlas_host_operations.errors import HostOperationError, InputError
from atlas_host_operations.registry import HTTPRegistryClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="operation-status")
    parser.add_argument("operation_id")
    args = parser.parse_args(argv)
    try:
        profile = os.environ.get("ATLAS_REGISTRY_PROFILE")
        if profile is None:
            raise InputError("ATLAS_REGISTRY_PROFILE is required")
        operation = HTTPRegistryClient.from_profile(profile).get_operation(
            args.operation_id
        )
        if operation is None:
            raise InputError(f"operation not found: {args.operation_id}")
        write_json(operation.model_dump(mode="json", by_alias=True))
        return 0
    except HostOperationError as exc:
        print(str(exc), file=sys.stderr)
        return _exception_exit_code(exc)
