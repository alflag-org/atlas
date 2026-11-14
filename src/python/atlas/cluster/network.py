"""Bounded TCP exchanges and UDP inbox, with DNS resolution on every attempt."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from atlas_core.cluster import ClusterError, PeerEndpoint

from .wire import MAX_DATAGRAM, Cipher, read_frame, write_frame

CONNECTION_LIMIT = 16


async def resolve(peer: PeerEndpoint, socktype: int, family: int = 0) -> list[tuple]:
    return await asyncio.get_running_loop().getaddrinfo(
        peer.host,
        peer.port,
        family=family,
        type=socktype,
    )


async def exchange(
    peer: PeerEndpoint,
    cipher: Cipher,
    method: str,
    data: dict,
    *,
    timeout: float,
) -> dict:
    try:
        async with asyncio.timeout(timeout):
            addresses = await resolve(peer, socket.SOCK_STREAM)
            last_error = "peer_unavailable"
            for family, socktype, protocol, _, address in addresses:
                connection = socket.socket(family, socktype, protocol)
                connection.setblocking(False)
                writer = None
                try:
                    # One blackholed DNS result must not consume every candidate's budget.
                    async with asyncio.timeout(timeout / max(1, len(addresses))):
                        await asyncio.get_running_loop().sock_connect(
                            connection, address
                        )
                        reader, writer = await asyncio.open_connection(sock=connection)
                        kind, challenge = cipher.open(await read_frame(reader))
                        if kind != "challenge" or not isinstance(
                            challenge.get("token"), str
                        ):
                            raise ClusterError("invalid_response")
                        request_id = uuid4().hex
                        await write_frame(
                            writer,
                            cipher.seal(
                                "request",
                                {
                                    "token": challenge["token"],
                                    "id": request_id,
                                    "method": method,
                                    "data": data,
                                },
                            ),
                        )
                        kind, reply = cipher.open(await read_frame(reader))
                        if kind != "response" or reply.get("id") != request_id:
                            raise ClusterError("invalid_response")
                        if "error" in reply:
                            raise ClusterError(str(reply["error"]))
                        if not isinstance(reply.get("result"), dict):
                            raise ClusterError("invalid_response")
                        return reply["result"]
                except ClusterError as exc:
                    last_error = exc.code
                except (OSError, TimeoutError, asyncio.IncompleteReadError):
                    last_error = "peer_unavailable"
                finally:
                    if writer is not None:
                        writer.close()
                    connection.close()
            raise ClusterError(last_error)
    except (OSError, TimeoutError):
        raise ClusterError("peer_unavailable") from None


class DatagramInbox(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[bytes, tuple]] = asyncio.Queue(maxsize=256)

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if len(data) <= MAX_DATAGRAM and not self.queue.full():
            self.queue.put_nowait((data, addr))


@asynccontextmanager
async def listen(
    bind: PeerEndpoint,
    get_cipher: Callable[[], Cipher | None],
    handler: Callable[[str, dict], Awaitable[dict]],
    *,
    timeout: float,
):
    tasks: set[asyncio.Task] = set()
    closing = False

    async def accept(reader, writer) -> None:
        try:
            async with asyncio.timeout(timeout):
                cipher = get_cipher()
                if cipher is None:
                    return
                token = uuid4().hex
                await write_frame(writer, cipher.seal("challenge", {"token": token}))
                kind, request = cipher.open(await read_frame(reader))
                request_id = request.get("id")
                if (
                    kind != "request"
                    or request.get("token") != token
                    or not isinstance(request_id, str)
                    or len(request_id) != 32
                    or not isinstance(request.get("method"), str)
                    or not isinstance(request.get("data"), dict)
                ):
                    raise ClusterError("invalid_request")
                try:
                    result = await handler(request["method"], request["data"])
                    reply = {"id": request_id, "result": result}
                except ClusterError as exc:
                    reply = {"id": request_id, "error": exc.code}
                await write_frame(writer, cipher.seal("response", reply))
        except (ClusterError, OSError, TimeoutError, asyncio.IncompleteReadError):
            pass  # Malformed connections have no trusted response channel.

    # Enforce admission before allocating a task, including unauthenticated clients.
    async with asyncio.TaskGroup() as group:

        def connected(reader, writer) -> None:
            if closing or len(tasks) >= CONNECTION_LIMIT:
                writer.close()
                return
            task = group.create_task(accept(reader, writer))
            tasks.add(task)

            def finished(task: asyncio.Task) -> None:
                # A task cancelled before its first step never enters accept's body.
                # The connection therefore belongs to this completion callback.
                writer.close()
                tasks.discard(task)

            task.add_done_callback(finished)

        server = await asyncio.start_server(connected, bind.host, bind.port)
        transport = None
        try:
            family = socket.AF_INET6 if ":" in bind.host else socket.AF_INET
            inbox = DatagramInbox()
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: inbox,
                local_addr=(bind.host, bind.port),
                family=family,
            )
            yield transport, inbox, family
        finally:
            closing = True
            server.close()
            if transport is not None:
                transport.close()
            for task in tasks:
                task.cancel()
            await server.wait_closed()
