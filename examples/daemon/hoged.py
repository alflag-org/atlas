"""Run with ``python -m hoged`` from this directory after installing Atlas."""

from atlas_core.daemon import DaemonContext, run


async def main(ctx: DaemonContext) -> None:
    ctx.logger.info("starting")
    try:
        ctx.ready()
        ctx.logger.info("ready")
        await ctx.wait_for_stop()
    finally:
        ctx.logger.info("stopped")


if __name__ == "__main__":
    raise SystemExit(run(main, name="hoged"))
