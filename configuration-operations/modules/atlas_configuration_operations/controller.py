"""Public configctl parser and executable composition."""

from __future__ import annotations

import argparse
import sys

from atlas_configuration_operations.child import job_argv, run_child

_JOBS = {
    "validate": "config-validate",
    "check": "config-check",
    "diff": "config-diff",
    "apply": "config-apply",
    "inventory": "inventory-show",
}


def _targets(argv_targets: list[str]) -> list[str]:
    candidates = list(argv_targets)
    if not sys.stdin.isatty():
        candidates.extend(sys.stdin.read().splitlines())
    seen: set[str] = set()
    targets: list[str] = []
    for candidate in candidates:
        target = candidate.strip()
        if target and target not in seen:
            seen.add(target)
            targets.append(target)
    return targets


def _diff_many(playbook: str, argv_targets: list[str]) -> int:
    targets = _targets(argv_targets)
    if not targets:
        print("at least one target is required", file=sys.stderr)
        return 2
    first_failure = 0
    for target in targets:
        print(f"==> {target} <==", file=sys.stderr)
        return_code = run_child(["configctl", "diff", playbook, target])
        if first_failure == 0 and return_code != 0:
            first_failure = return_code
    return first_failure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="configctl")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("playbook")
    for command in ("check", "diff", "apply"):
        child = subparsers.add_parser(command)
        child.add_argument("playbook")
        child.add_argument("target")
    diff_many = subparsers.add_parser("diff-many")
    diff_many.add_argument("playbook")
    diff_many.add_argument("targets", nargs="*")
    subparsers.add_parser("inventory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "diff-many":
        return _diff_many(args.playbook, args.targets)
    child_args: list[str]
    if args.command == "inventory":
        child_args = []
    elif args.command == "validate":
        child_args = [args.playbook]
    else:
        child_args = [args.playbook, args.target]
    return run_child(job_argv(_JOBS[args.command], child_args))
