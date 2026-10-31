"""Local operator commands; credentials travel in files, never arguments."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from pathlib import Path

from atlas.yamlutil import load_yaml_file
from atlas_core.cluster import ClusterClient, ClusterError


def private_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Cluster input must be an owner-only regular file")
        value = json.loads(stream.read(1024 * 1024 + 1))
    if not isinstance(value, dict):
        raise TypeError("Cluster input must be an object")
    return value


def write_private(path: Path, result: dict) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def command(args: argparse.Namespace) -> int:
    params = {}
    method = args.cluster_action
    try:
        if method == "init":
            params = {"name": args.name}
        elif method == "invite":
            ttl = args.ttl
            multiplier = 60 if ttl.endswith("m") else 1
            params = {
                "ttl_seconds": int(ttl.removesuffix("m").removesuffix("s"))
                * multiplier,
                "permissions": args.permission or ["read", "execute"],
                "labels": dict(value.split("=", 1) for value in args.label),
            }
        elif method == "join":
            params = {
                "invite": private_json(args.invite_file),
                "reconcile": args.reconcile,
            }
        elif method == "remove":
            params = {"node_id": args.node_id}
        elif method == "plan":
            workflow = load_yaml_file(args.workflow)
            if workflow.get("name") != args.name:
                raise ValueError("workflow name does not match")
            params = {"workflow": workflow}
        elif method == "apply":
            params = private_json(args.plan) | {"confirm": args.confirm}
        elif method == "operation":
            method = "operation." + args.operation_action
            params = {"operation_id": args.operation_id}
        result = asyncio.run(
            ClusterClient(args.socket, timeout=300).request(method, params)
        )
        if method in {"invite", "plan"}:
            write_private(args.output, result)
            print(f"{method} written: {args.output}")
            if method == "plan":
                print(f"digest: {result['digest']}")
        else:
            print(json.dumps(result, sort_keys=True))
        return 0
    except ClusterError as exc:
        print(
            json.dumps({"schema": "atlas.cluster-error/v1", "error": exc.code}),
            file=sys.stderr,
        )
        return 2


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "cluster", help="manage an explicitly initialized Cluster"
    )
    parser.add_argument("--socket", type=Path)
    actions = parser.add_subparsers(dest="cluster_action", required=True)
    for action in (
        "status",
        "members",
        "leave",
        "init",
        "join",
        "remove",
        "invite",
        "plan",
        "apply",
        "operation",
    ):
        sub = actions.add_parser(action)
        sub.set_defaults(func=command)
        if action == "init":
            sub.add_argument("--name", required=True)
        elif action == "join":
            sub.add_argument("--invite-file", type=Path, required=True)
            sub.add_argument("--reconcile", action="store_true")
        elif action == "remove":
            sub.add_argument("node_id")
        elif action == "invite":
            sub.add_argument("invite_action", choices=["create"])
            sub.add_argument("--ttl", default="10m")
            sub.add_argument("--output", type=Path, required=True)
            sub.add_argument(
                "--permission", action="append", choices=["read", "execute", "admin"]
            )
            sub.add_argument(
                "--label", action="append", default=[], metavar="KEY=VALUE"
            )
        elif action == "plan":
            sub.add_argument("name")
            sub.add_argument("--workflow", type=Path, required=True)
            sub.add_argument("--out", dest="output", type=Path, required=True)
        elif action == "apply":
            sub.add_argument("plan", type=Path)
            sub.add_argument("--confirm", required=True)
        elif action == "operation":
            sub.add_argument("operation_action", choices=["inspect", "reconcile"])
            sub.add_argument("operation_id")
