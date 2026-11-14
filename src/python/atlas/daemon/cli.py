"""Operator commands for host-declared daemon instances."""

from __future__ import annotations

import argparse
import json
import sys

from atlas.paths import get_paths

from .config import load_instance
from .systemd import Systemd, audit


def command(args: argparse.Namespace) -> int:
    directory = get_paths().etc / "daemons.d"
    try:
        if args.action == "list":
            for path in sorted(directory.glob("*.yml")):
                print(load_instance(directory, path.stem).name)
            return 0
        instance = load_instance(
            directory,
            args.instance,
            check_resources=args.action in {"validate", "install", "start", "restart"},
        )
        adapter = Systemd()
        if args.action == "inspect":
            result = instance.to_dict()
        elif args.action == "validate":
            result = {"name": instance.name, "valid": True}
        elif args.action == "status":
            result = adapter.status(instance)
        elif args.action == "logs":
            return adapter.logs(instance, args.since)
        else:
            if args.action in {"install", "remove"}:
                adapter.install(instance, remove=args.action == "remove")
            else:
                adapter.action(instance, args.action)
            audit(instance, args.action)
            result = {"name": instance.name, "action": args.action}
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            for key, value in result.items():
                print(f"{key}: {value}")
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        code = (
            "invalid_configuration"
            if isinstance(exc, (TypeError, ValueError))
            else "operation_failed"
        )
        if args.json:
            print(
                json.dumps({"schema": "atlas.daemon-error/v1", "error": code}),
                file=sys.stderr,
            )
        else:
            print(f"atlas daemon: {exc}", file=sys.stderr)
        return 2


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "daemon", help="manage explicitly configured host daemons"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for action in (
        "list",
        "inspect",
        "validate",
        "install",
        "remove",
        "enable",
        "disable",
        "start",
        "stop",
        "restart",
        "status",
        "logs",
    ):
        sub = actions.add_parser(action)
        sub.set_defaults(func=command, json=False)
        if action != "list":
            sub.add_argument("instance")
        if action in {"inspect", "status"}:
            sub.add_argument("--json", action="store_true")
        if action == "logs":
            sub.add_argument("--since")
