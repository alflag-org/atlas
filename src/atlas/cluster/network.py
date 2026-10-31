"""Internal bounded TLS RPC; certificates do not replace live authorization."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Awaitable, Callable
from uuid import uuid4

from atlas_core.ipc import IPCError, Unavailable, read_message, write_message

from .identity import endpoint, peer_identity

VERSION = "atlas.cluster-peer/v1"


async def call(
    address: str,
    node_id: str,
    context: ssl.SSLContext,
    method: str,
    params: dict,
    *,
    timeout: float = 30,
) -> dict:
    request_id = str(uuid4())
    try:
        async with asyncio.timeout(timeout):
            host, port = endpoint(address)
            reader, writer = await asyncio.open_connection(
                host, port, ssl=context, server_hostname=f"{node_id}.invalid"
            )
            try:
                await write_message(
                    writer,
                    {
                        "api_version": VERSION,
                        "request_id": request_id,
                        "method": method,
                        "params": params,
                    },
                )
                result = await read_message(reader)
            finally:
                writer.close()
                await writer.wait_closed()
    except (OSError, TimeoutError, asyncio.IncompleteReadError):
        raise Unavailable() from None
    if result.get("api_version") != VERSION or result.get("request_id") != request_id:
        raise IPCError("invalid_response")
    if "error" in result:
        raise IPCError(result["error"])
    if not isinstance(result.get("result"), dict):
        raise IPCError("invalid_response")
    return result["result"]


class Listener:
    def __init__(
        self, handler: Callable[[str, dict, tuple[str, str] | None], Awaitable[dict]]
    ) -> None:
        self.handler = handler
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task] = set()

    async def start(self, address: str, context: ssl.SSLContext) -> None:
        host, port = endpoint(address)
        self.server = await asyncio.start_server(
            self.connection,
            host,
            port,
            ssl=context,
            ssl_handshake_timeout=5,
            backlog=32,
        )

    async def connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with asyncio.timeout(300):
                if len(self.tasks) > 32:
                    return
                certificate = writer.get_extra_info("ssl_object").getpeercert(
                    binary_form=True
                )
                peer = peer_identity(certificate) if certificate else None
                async with asyncio.timeout(10):
                    request = await read_message(reader)
                request_id = request.get("request_id")
                if not isinstance(request_id, str) or len(request_id) > 128:
                    raise IPCError("invalid_message")
                reply = {"api_version": VERSION, "request_id": request_id}
                try:
                    if request.get("api_version") != VERSION:
                        raise IPCError("unsupported_version")
                    if not isinstance(request.get("method"), str) or not isinstance(
                        request.get("params"), dict
                    ):
                        raise IPCError("invalid_message")
                    reply["result"] = await self.handler(
                        request["method"], request["params"], peer
                    )
                except IPCError as exc:
                    reply["error"] = exc.code
                except Exception:
                    reply["error"] = "internal_error"
                await write_message(writer, reply)
        except (OSError, TimeoutError, IPCError, asyncio.IncompleteReadError):
            pass  # The peer has no usable response channel.
        finally:
            self.tasks.remove(task)
            writer.close()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()

        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()
