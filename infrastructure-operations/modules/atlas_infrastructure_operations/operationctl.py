"""Public operation artifact and durable-state diagnostic controller."""

from __future__ import annotations

import argparse

from atlas_infrastructure_operations.child import run_job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="operationctl")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "inspect"):
        child = subparsers.add_parser(command)
        child.add_argument("artifact", nargs="?", default="-")
    status = subparsers.add_parser("status")
    status.add_argument("operation_id")
    args = parser.parse_args(argv)
    if args.command == "status":
        return run_job("operation-status", [args.operation_id])
    return run_job(f"operation-artifact-{args.command}", [args.artifact])
