"""Test-only entry point: real daemon lifecycle with shorter protocol timers."""

import asyncio
import os
import sys
from pathlib import Path

# Collect coverage in actual child processes when the parent is measuring it.
coverage = None
if os.environ.get("ATLAS_TEST_COVERAGE"):
    from coverage import Coverage

    coverage = Coverage(
        data_file=os.environ["ATLAS_TEST_COVERAGE"], branch=True, source=["src"]
    )
    coverage.start()


def main():
    if os.environ.get("ATLAS_TEST_ENTRY"):
        import runpy

        from atlas_core.logging import configure_logging

        config_path = sys.argv[1]
        configure_logging("cluster", path=Path(config_path).parent / "daemon.log")
        sys.argv = ["atlasd", "--config", config_path]
        runpy.run_module("atlas.cluster.daemon", run_name="__main__")
    from atlas.cluster.config import load_config
    from atlas.cluster.daemon import application
    from atlas.cluster.node import Timers
    from atlas_core.daemon import run
    from atlas_core.logging import configure_logging

    configure_logging("cluster", path=Path(sys.argv[1]).parent / "daemon.log")
    config = load_config(Path(sys.argv[1]))
    timers = Timers(0.15, 0.12, 1.2, 0.08, 0.25, 0.6, 4)

    async def start(ctx):
        # Preserve each simulated host's source address through the test gateway.
        loop = asyncio.get_running_loop()
        connect = loop.sock_connect

        async def connect_from_host(sock, address):
            sock.bind((config.bind.host, 0))
            await connect(sock, address)

        loop.sock_connect = connect_from_host
        await application(ctx, config, timers)

    return run(start, name="cluster", shutdown_timeout=3)


if __name__ == "__main__":
    try:
        status = main()
    finally:
        if coverage is not None:
            coverage.stop()
            coverage.save()
    raise SystemExit(status)
