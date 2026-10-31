"""Membership commands and the fixed SSH registry entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from atlas_core.cluster import ClusterError

from .node import Node, local_member
from .store import MAX_BYTES


def command(args: argparse.Namespace) -> int:
    try:
        node = Node(args.directory)
        action = args.cluster_action
        if action == "init":
            result = node.initialize(
                args.name,
                local_member(
                    args.address, dict(item.split("=", 1) for item in args.label)
                ),
            )
        elif action == "join":
            result = asyncio.run(
                node.join(
                    args.via,
                    local_member(
                        args.address, dict(item.split("=", 1) for item in args.label)
                    ),
                )
            )
        elif action == "leave":
            result = asyncio.run(node.leave(args.member))
        elif action == "status":
            result = asyncio.run(node.status(cached=args.cached))
        else:
            snapshot, reachable = asyncio.run(node.snapshot(cached=args.cached))
            result = snapshot | {"stale": reachable is not True}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ClusterError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, ClusterError) else "invalid_configuration"
        print(
            json.dumps({"schema": "atlas.cluster-error/v1", "error": code}),
            file=sys.stderr,
        )
        return 2


def registry_command(args: argparse.Namespace) -> int:
    reply = {"schema": "atlas.membership-rpc/v1"}
    try:
        data = sys.stdin.read(MAX_BYTES + 1)
        request = json.loads(data)
        if len(data) > MAX_BYTES or not isinstance(request, dict):
            raise ValueError
        reply["result"] = Node(args.directory).registry_request(request)
    except (ClusterError, OSError, ValueError) as exc:
        reply["error"] = (
            exc.code if isinstance(exc, ClusterError) else "invalid_request"
        )
    print(json.dumps(reply, sort_keys=True))
    return 0


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("cluster", help="manage host membership over SSH")
    parser.add_argument(
        "--directory", type=Path, help="local membership state directory"
    )
    actions = parser.add_subparsers(
        dest="cluster_action", required=True, metavar="{init,join,leave,members,status}"
    )
    for action in ("init", "join", "leave", "members", "status"):
        sub = actions.add_parser(action)
        sub.set_defaults(func=command)
        if action in {"init", "join"}:
            sub.add_argument(
                "--address", help="member SSH destination; defaults to the host FQDN"
            )
            sub.add_argument(
                "--label", action="append", default=[], metavar="KEY=VALUE"
            )
        if action == "init":
            sub.add_argument("--name", required=True)
        elif action == "join":
            sub.add_argument("--via", required=True, help="registry SSH destination")
        elif action == "leave":
            sub.add_argument(
                "--member", help="remove this host ID instead of the local member"
            )
        else:
            sub.add_argument(
                "--cached",
                action="store_true",
                help="read without contacting the registry",
            )
    internal = actions.add_parser("_registry")
    internal.set_defaults(func=registry_command)
