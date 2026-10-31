"""The independent atlasd foreground entry point."""

from __future__ import annotations

import argparse
import os

from atlas_core.daemon import DaemonContext, run
from atlas_core.ipc import serve

from .node import Node


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlasd")
    parser.add_argument("--name", default="atlasd", help="local daemon instance name")
    parser.add_argument(
        "--endpoint", help="advertised bind address, e.g. tls://192.0.2.10:7443"
    )
    parser.add_argument(
        "--display-name",
        default="node",
        help="display only; never used for authentication",
    )
    args = parser.parse_args(argv)

    async def application(ctx: DaemonContext) -> None:
        node = Node(ctx.state_dir, args.endpoint, args.display_name)
        try:
            await node.start()
            async with serve(
                ctx.runtime_dir / "control.sock",
                api_version="atlas.cluster/v1",
                handler=node.local,
                authorize=lambda peer, method: peer.uid == os.getuid(),
                timeout=300,
            ):
                ctx.ready()
                await ctx.wait_for_stop()
        finally:
            await node.close()

    return run(application, name=args.name)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
