import os

from atlas_core.daemon import DaemonContext, run
from atlas_core.ipc import IPCError, Peer, serve


async def status(method: str, params: dict, peer: Peer) -> dict:
    if method != "status":
        raise IPCError("unknown_method")
    return {"message": "ready"}


async def main(ctx: DaemonContext) -> None:
    async with serve(
        ctx.runtime_dir / "control.sock",
        api_version="sample.service/v1",
        handler=status,
        authorize=lambda peer, method: peer.uid == os.getuid(),
    ):
        ctx.ready()
        await ctx.wait_for_stop()


if __name__ == "__main__":
    raise SystemExit(run(main, name="sample-service"))
