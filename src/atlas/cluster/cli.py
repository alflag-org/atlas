"""Cluster CLI: local IPC only, with JSON results and stderr diagnostics."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from atlas_core.cluster import ClusterClient, ClusterError


def command(args: argparse.Namespace) -> int:
    params = {}
    if args.cluster_action == "init":
        params = {"name": args.name}
    elif args.cluster_action == "join":
        params = {"seeds": args.seeds}
    try:
        result = asyncio.run(
            ClusterClient(args.socket).request(args.cluster_action, params)
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except ClusterError as exc:
        print(
            json.dumps({"schema": "atlas.cluster-error/v2", "error": exc.code}),
            file=sys.stderr,
        )
        return 2


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "cluster", help="query and control local atlasd membership"
    )
    parser.add_argument("--socket", type=Path, help="atlasd Unix socket")
    actions = parser.add_subparsers(dest="cluster_action", required=True)
    for action in ("init", "join", "leave", "members", "status"):
        sub = actions.add_parser(action)
        sub.set_defaults(func=command)
        if action == "init":
            sub.add_argument("--name", required=True)
        elif action == "join":
            sub.add_argument("seeds", nargs="+", help="peer FQDN/IP with optional port")
