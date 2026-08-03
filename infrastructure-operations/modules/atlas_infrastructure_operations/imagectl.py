"""Public machine-image lifecycle controller."""

from __future__ import annotations

import argparse

from atlas_infrastructure_operations.child import run_job


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="imagectl")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("provider")
    plan.add_argument("input")
    apply = subparsers.add_parser("apply")
    apply.add_argument("provider")
    apply.add_argument("artifact", nargs="?", default="-")
    apply.add_argument("--confirm", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("provider")
    verify.add_argument("artifact", nargs="?", default="-")
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("provider")
    rollback.add_argument("artifact", nargs="?", default="-")
    rollback.add_argument("--confirm", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plan":
        return run_job("vm-template-create-plan", [args.provider, args.input])
    job_args = [args.provider, args.artifact]
    if args.command in {"apply", "rollback"}:
        job_args.extend(["--confirm", args.confirm])
    return run_job(f"vm-template-create-{args.command}", job_args)
