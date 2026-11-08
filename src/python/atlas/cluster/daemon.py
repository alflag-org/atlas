"""atlasd is a foreground application of the generic Daemon SDK."""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
from dataclasses import asdict
from pathlib import Path

from atlas_core.cluster import API_VERSION, ClusterError, ClusterMember, MemberState
from atlas_core.daemon import DaemonContext, run
from atlas_core.host import get_host
from atlas_core.ipc import IPCError, serve

from .config import Config, load_config
from .membership import Membership
from .model import record
from .network import listen
from .node import DEFAULT_TIMERS, Node, Timers
from .store import Store


async def application(
    ctx: DaemonContext, config: Config, timers: Timers = DEFAULT_TIMERS
) -> None:
    host = get_host()
    local = record(
        asdict(
            ClusterMember(
                host.id,
                socket.getfqdn(),
                config.advertise,
                host.role,
                host.site,
                config.labels or {},
                0,
                MemberState.ALIVE,
            )
        )
    )
    store = Store(ctx.state_dir)
    with store.locked():
        membership = Membership(store, store.read(host.id), local, timers.suspicion)
        node = Node(config, membership, timers)
        if node.state.cluster_id is not None:
            node.cipher = node.credentials()
            membership.restart()

        async def control(method, params, peer):
            try:
                return await node.control(method, params, peer)
            except ClusterError as exc:
                raise IPCError(exc.code) from None
            except OSError:
                raise IPCError("configuration_or_storage_error") from None

        async with listen(
            config.bind,
            lambda: node.cipher,
            node.remote,
            timeout=timers.exchange_timeout,
        ) as (transport, inbox, family):
            node.transport, node.family = transport, family
            async with serve(
                ctx.runtime_dir / "control.sock",
                api_version=API_VERSION,
                handler=control,
                authorize=lambda peer, method: peer.uid == os.getuid(),
                timeout=timers.join_timeout + 2,
            ):
                async with asyncio.TaskGroup() as group:
                    tasks = [
                        group.create_task(node.datagrams(inbox, group)),
                        group.create_task(node.probes()),
                        group.create_task(node.gossip()),
                        group.create_task(node.sync()),
                    ]
                    ctx.logger.info(
                        "cluster listening on %s:%s", config.bind.host, config.bind.port
                    )
                    ctx.ready()
                    await ctx.wait_for_stop()
                    for task in (*tasks, *node.relays):
                        task.cancel()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlasd")
    parser.add_argument("--config", type=Path, default=Path("/etc/atlas/cluster.yml"))
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ClusterError):
        from atlas_core.logging import get_logger

        get_logger("cluster").error("invalid cluster configuration")
        return 2

    async def start(ctx):
        try:
            await application(ctx, config)
        except ClusterError as exc:
            ctx.logger.error("cluster failed: %s", exc.code)
            raise

    return run(start, name="cluster")


if __name__ == "__main__":
    raise SystemExit(main())
