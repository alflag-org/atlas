"""Public read-only provider diagnostic controller."""

from __future__ import annotations

import argparse

from atlas_infrastructure_operations.child import run_job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="providerctl")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "status"):
        child = subparsers.add_parser(command)
        child.add_argument("provider")
    args = parser.parse_args(argv)
    job = "provider-validate" if args.command == "validate" else "proxmox-status"
    return run_job(job, [args.provider])
